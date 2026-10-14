"""Immutable, fee-aware shadow evaluation of one binary market's displayed book.

This module is deliberately an observation model.  It can rank a complete
displayed YES/NO bundle only; it has no order, account, or execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal

ONE = Decimal("1")
CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")
TAKER_COEFFICIENT = Decimal("0.07")
MAKER_COEFFICIENT = Decimal("0.0175")
FEE_MODEL_IDENTITY = "kalshi-quadratic-taker-v1"
ROUNDING_IDENTITY = (
    "per-displayed-slice-centicent-trade-fee-then-cent-balance-rounding-no-rebate-v1"
)
SCHEDULE_IDENTITY = "kalshi-fee-schedule-2026-07-07"
SCHEDULE_EFFECTIVE_DATE = "2026-07-07"


class OrderbookSide(StrEnum):
    YES_BID = "YES_BID"
    NO_BID = "NO_BID"


class ComplementState(StrEnum):
    CANDIDATE = "CANDIDATE"
    NO_CANDIDATE = "NO_CANDIDATE"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"


class ComplementReasonCode(StrEnum):
    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
    RULE_EVIDENCE_MISSING = "RULE_EVIDENCE_MISSING"
    SERIES_EVIDENCE_MISSING = "SERIES_EVIDENCE_MISSING"
    ORDERBOOK_MISSING_SIDE = "ORDERBOOK_MISSING_SIDE"
    ORDERBOOK_LEVEL_INVALID = "ORDERBOOK_LEVEL_INVALID"
    FEE_MODEL_UNSUPPORTED = "FEE_MODEL_UNSUPPORTED"
    FEE_EVIDENCE_STALE = "FEE_EVIDENCE_STALE"
    ROUNDING_ASSUMPTION_UNSUPPORTED = "ROUNDING_ASSUMPTION_UNSUPPORTED"
    NO_MATCHED_QUANTITY = "NO_MATCHED_QUANTITY"
    GROSS_GAP_NOT_POSITIVE = "GROSS_GAP_NOT_POSITIVE"
    FEES_ELIMINATE_GROSS_GAP = "FEES_ELIMINATE_GROSS_GAP"
    ROUNDING_RESERVE_ELIMINATES_GAP = "ROUNDING_RESERVE_ELIMINATES_GAP"
    NET_LOWER_BOUND_NOT_POSITIVE = "NET_LOWER_BOUND_NOT_POSITIVE"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_decimal(value: Decimal) -> Decimal:
    # PostgreSQL returns the declared Numeric(38,18) display scale; immutable
    # hashes and domain validation retain only the authoritative fixed value.
    return parse_decimal(format(value.normalize(), "f"), allow_negative=True)


def _ceil_to(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def _descending_timestamp_key(value: datetime) -> int:
    """A stable UTC recency key without converting authoritative time through float."""

    normalized = _aware(value, "evaluated_at")
    microseconds = (
        (((normalized.toordinal() * 24 + normalized.hour) * 60 + normalized.minute) * 60)
        + normalized.second
    ) * 1_000_000 + normalized.microsecond
    return -microseconds


def _conservative_per_contract_lower_bound(total: Decimal, quantity: Decimal) -> Decimal:
    """Bound a derived ratio to Apex precision without making it less conservative."""

    exact = total / quantity
    if exact == 0:
        return Decimal(0)
    # Apex authoritative decimals permit at most eight fractional digits.
    scale = min(8, max(0, 18 - exact.copy_abs().adjusted() - 1))
    return exact.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_FLOOR)


@dataclass(frozen=True, slots=True)
class PublicOrderbookLevel:
    """One provider bid level; its reciprocal ask is derived, never supplied."""

    level_id: str
    orderbook_snapshot_id: str
    side: OrderbookSide
    ordinal: int
    bid_price: Decimal
    quantity: Decimal
    order_count: int | None
    source_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.level_id or not self.orderbook_snapshot_id:
            raise ValueValidationError("orderbook level identities must not be empty")
        if self.ordinal < 0:
            raise ValueValidationError("orderbook level ordinal must not be negative")
        bid_price = parse_decimal(self.bid_price)
        quantity = parse_decimal(self.quantity)
        if not Decimal(0) <= bid_price <= ONE:
            raise ValueValidationError("orderbook bid price must be in the binary range")
        if quantity <= 0:
            raise ValueValidationError("orderbook level quantity must be positive")
        if self.order_count is not None and self.order_count < 0:
            raise ValueValidationError("orderbook level order count must not be negative")
        object.__setattr__(self, "bid_price", bid_price)
        object.__setattr__(self, "quantity", quantity)

    @property
    def derived_ask_side(self) -> str:
        return "NO" if self.side is OrderbookSide.YES_BID else "YES"

    @property
    def derived_ask_price(self) -> Decimal:
        return ONE - self.bid_price

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class PublicOrderbookSnapshot:
    orderbook_snapshot_id: str
    ingestion_run_id: str
    raw_response_id: str
    market_snapshot_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    provider_event_ticker: str | None
    series_ticker: str
    observed_at: datetime
    maximum_depth: int
    yes_bid_level_count: int
    no_bid_level_count: int
    data_quality_state: str
    canonical_source_hash: ContentHash

    def __post_init__(self) -> None:
        if (
            not self.orderbook_snapshot_id
            or not self.ingestion_run_id
            or not self.raw_response_id
            or not self.market_snapshot_id
            or not self.provider_market_ticker
            or not self.series_ticker
        ):
            raise ValueValidationError("orderbook snapshot identities must not be empty")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if not 1 <= self.maximum_depth <= 100:
            raise ValueValidationError("orderbook maximum depth must be between one and 100")
        if min(self.yes_bid_level_count, self.no_bid_level_count) < 0:
            raise ValueValidationError("orderbook level counts must not be negative")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class SeriesFeeSnapshot:
    fee_snapshot_id: str
    ingestion_run_id: str
    raw_response_id: str
    provider: PublicMarketProvider
    series_ticker: str
    observed_at: datetime
    fee_type: str | None
    fee_multiplier: Decimal | None
    effective_at: datetime | None
    fee_model_identity: str
    taker_coefficient: Decimal
    maker_coefficient: Decimal
    rounding_identity: str
    schedule_identity: str
    schedule_effective_date: str
    source_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.fee_snapshot_id or not self.ingestion_run_id or not self.raw_response_id:
            raise ValueValidationError("fee snapshot identities must not be empty")
        if self.provider is not PublicMarketProvider.KALSHI or not self.series_ticker:
            raise ValueValidationError("fee snapshot must retain its exact Kalshi series")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.effective_at is not None:
            object.__setattr__(self, "effective_at", _aware(self.effective_at, "effective_at"))
        if self.fee_multiplier is not None:
            multiplier = parse_decimal(self.fee_multiplier)
            if multiplier < 0:
                raise ValueValidationError("fee multiplier must not be negative")
            object.__setattr__(self, "fee_multiplier", multiplier)
        object.__setattr__(self, "taker_coefficient", parse_decimal(self.taker_coefficient))
        object.__setattr__(self, "maker_coefficient", parse_decimal(self.maker_coefficient))

    @property
    def is_supported(self) -> bool:
        return (
            self.fee_type == "quadratic"
            and self.fee_multiplier is not None
            and self.fee_model_identity == FEE_MODEL_IDENTITY
            and self.taker_coefficient == TAKER_COEFFICIENT
            and self.maker_coefficient == MAKER_COEFFICIENT
            and self.rounding_identity == ROUNDING_IDENTITY
            and self.schedule_identity == SCHEDULE_IDENTITY
            and self.schedule_effective_date == SCHEDULE_EFFECTIVE_DATE
        )

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class ComplementFillSlice:
    slice_id: str
    evaluation_id: str
    ordinal: int
    purchased_side: str
    source_level_id: str
    quantity: Decimal
    ask_price: Decimal
    acquisition_cost: Decimal
    modeled_trade_fee: Decimal
    rounding_reserve: Decimal

    def __post_init__(self) -> None:
        if not self.slice_id or not self.evaluation_id or not self.source_level_id:
            raise ValueValidationError("complement slice identities must not be empty")
        if self.ordinal < 0 or self.purchased_side not in {"YES", "NO"}:
            raise ValueValidationError("complement slice has invalid side or ordinal")
        for name in (
            "quantity",
            "ask_price",
            "acquisition_cost",
            "modeled_trade_fee",
            "rounding_reserve",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        if self.quantity <= 0 or not Decimal(0) <= self.ask_price <= ONE:
            raise ValueValidationError("complement slice has invalid quantity or ask")
        if self.acquisition_cost != self.quantity * self.ask_price:
            raise ValueValidationError("complement slice acquisition cost must be exact")
        if self.modeled_trade_fee < 0 or self.rounding_reserve < 0:
            raise ValueValidationError("complement slice fees must not be negative")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class BinaryComplementEvaluation:
    evaluation_id: str
    ingestion_run_id: str
    orderbook_snapshot_id: str
    market_snapshot_id: str
    fee_snapshot_id: str | None
    provider: PublicMarketProvider
    provider_market_ticker: str
    evaluator_version: str
    evaluated_at: datetime
    maximum_depth: int
    maximum_bundle_quantity: Decimal
    rounding_identity: str
    matched_quantity: Decimal
    weighted_yes_ask: Decimal | None
    weighted_no_ask: Decimal | None
    gross_acquisition_cost: Decimal
    gross_settlement_value: Decimal
    gross_complement_gap: Decimal
    yes_modeled_taker_fee: Decimal
    no_modeled_taker_fee: Decimal
    modeled_taker_fees: Decimal
    conservative_rounding_reserve: Decimal
    total_modeled_acquisition_cost: Decimal
    conservative_net_lower_bound: Decimal
    net_lower_bound_per_contract: Decimal | None
    state: ComplementState
    reason_codes: tuple[ComplementReasonCode, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.evaluation_id or not self.orderbook_snapshot_id or not self.market_snapshot_id:
            raise ValueValidationError("complement evaluation identities must not be empty")
        object.__setattr__(self, "evaluated_at", _aware(self.evaluated_at, "evaluated_at"))
        if not 1 <= self.maximum_depth <= 100:
            raise ValueValidationError("complement evaluation depth is out of bounds")
        for name in (
            "maximum_bundle_quantity",
            "matched_quantity",
            "gross_acquisition_cost",
            "gross_settlement_value",
            "gross_complement_gap",
            "yes_modeled_taker_fee",
            "no_modeled_taker_fee",
            "modeled_taker_fees",
            "conservative_rounding_reserve",
            "total_modeled_acquisition_cost",
            "conservative_net_lower_bound",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        for name in ("weighted_yes_ask", "weighted_no_ask"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _canonical_decimal(value))
        if self.net_lower_bound_per_contract is not None:
            object.__setattr__(
                self,
                "net_lower_bound_per_contract",
                _canonical_decimal(self.net_lower_bound_per_contract),
            )
        if self.maximum_bundle_quantity <= 0 or self.matched_quantity < 0:
            raise ValueValidationError("complement evaluation quantities are invalid")
        if self.yes_modeled_taker_fee < 0 or self.no_modeled_taker_fee < 0:
            raise ValueValidationError("complement leg fees must not be negative")
        if self.modeled_taker_fees != self.yes_modeled_taker_fee + self.no_modeled_taker_fee:
            raise ValueValidationError("complement total fee must equal its two leg fees")
        if self.total_modeled_acquisition_cost != (
            self.gross_acquisition_cost
            + self.modeled_taker_fees
            + self.conservative_rounding_reserve
        ):
            raise ValueValidationError("complement modeled acquisition cost must be exact")
        if self.matched_quantity:
            expected = _conservative_per_contract_lower_bound(
                self.conservative_net_lower_bound, self.matched_quantity
            )
            if (
                self.net_lower_bound_per_contract is None
                or self.net_lower_bound_per_contract != expected
            ):
                raise ValueValidationError(
                    "complement per-contract lower bound must be conservative"
                )
        elif self.net_lower_bound_per_contract is not None:
            raise ValueValidationError(
                "zero matched quantity cannot have a per-contract lower bound"
            )
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))
        if self.state is ComplementState.CANDIDATE:
            if self.conservative_net_lower_bound <= 0 or self.reason_codes:
                raise ValueValidationError("candidate requires a positive clean lower bound")
        elif not self.reason_codes:
            raise ValueValidationError("non-candidate complement evaluation requires a reason")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


def _ask_levels(
    levels: tuple[PublicOrderbookLevel, ...], *, bid_side: OrderbookSide
) -> tuple[PublicOrderbookLevel, ...]:
    values = tuple(value for value in levels if value.side is bid_side)
    # The provider sends bids low-to-high.  Reciprocal asks must be bought
    # low-to-high, i.e. source bids high-to-low.  Verify strict source ordering
    # instead of silently repairing malformed evidence.
    if any(
        values[index].bid_price >= values[index + 1].bid_price for index in range(len(values) - 1)
    ):
        raise ValueValidationError("orderbook bid levels must be strictly ascending")
    return tuple(reversed(values))


def _fill_fee(
    *, quantity: Decimal, ask_price: Decimal, multiplier: Decimal
) -> tuple[Decimal, Decimal]:
    """Conservative per-displayed-slice fee with no accumulator rebate credit."""

    acquisition = quantity * ask_price
    raw_trade_fee = multiplier * TAKER_COEFFICIENT * quantity * ask_price * (ONE - ask_price)
    # Kalshi first rounds the fee itself up to a centicent.  For a buy, the
    # remaining balance alignment is the ceiling of cost plus that fee to a
    # cent, expressed as a non-negative conservative reserve.  No accumulator
    # rebate is credited because a public book cannot prove order grouping.
    modeled_trade_fee = _ceil_to(raw_trade_fee, CENTICENT)
    post_fee_cost = acquisition + modeled_trade_fee
    cent_total = _ceil_to(post_fee_cost, CENT)
    return modeled_trade_fee, cent_total - post_fee_cost


def model_public_taker_fee(
    *, quantity: Decimal, ask_price: Decimal, multiplier: Decimal
) -> tuple[Decimal, Decimal]:
    """Expose the frozen public-evidence fee and rounding model for complete sets."""

    normalized_quantity = parse_decimal(quantity, allow_zero=False)
    normalized_price = parse_decimal(ask_price)
    normalized_multiplier = parse_decimal(multiplier)
    if not Decimal(0) < normalized_price < ONE:
        raise ValueValidationError("public taker fee ask must be inside the binary range")
    return _fill_fee(
        quantity=normalized_quantity,
        ask_price=normalized_price,
        multiplier=normalized_multiplier,
    )


def _non_positive_reason(
    *, gross_gap: Decimal, modeled_fees: Decimal, rounding_reserve: Decimal
) -> ComplementReasonCode:
    if gross_gap <= 0:
        return ComplementReasonCode.GROSS_GAP_NOT_POSITIVE
    if gross_gap - modeled_fees <= 0:
        return ComplementReasonCode.FEES_ELIMINATE_GROSS_GAP
    if gross_gap - modeled_fees - rounding_reserve <= 0:
        return ComplementReasonCode.ROUNDING_RESERVE_ELIMINATES_GAP
    return ComplementReasonCode.NET_LOWER_BOUND_NOT_POSITIVE


def evaluate_binary_complement(
    *,
    orderbook: PublicOrderbookSnapshot,
    fee: SeriesFeeSnapshot | None,
    levels: tuple[PublicOrderbookLevel, ...],
    market_is_open: bool,
    rule_content_hash: ContentHash | None,
    maximum_bundle_quantity: Decimal,
    evaluated_at: datetime,
    evaluator_version: str = "public-binary-complement-v1",
) -> tuple[BinaryComplementEvaluation, tuple[ComplementFillSlice, ...]]:
    """Match reciprocal asks for one market only, without asserting execution."""

    maximum = parse_decimal(maximum_bundle_quantity, allow_zero=False)
    observed_at = _aware(evaluated_at, "evaluated_at")
    reasons: list[ComplementReasonCode] = []
    if not market_is_open:
        reasons.append(ComplementReasonCode.MARKET_NOT_OPEN)
    if rule_content_hash is None:
        reasons.append(ComplementReasonCode.RULE_EVIDENCE_MISSING)
    if fee is None:
        reasons.append(ComplementReasonCode.SERIES_EVIDENCE_MISSING)
    elif not fee.is_supported:
        reasons.append(ComplementReasonCode.FEE_MODEL_UNSUPPORTED)
    elif abs(observed_at - fee.observed_at) > timedelta(minutes=10):
        reasons.append(ComplementReasonCode.FEE_EVIDENCE_STALE)
    if fee is not None and fee.rounding_identity != ROUNDING_IDENTITY:
        reasons.append(ComplementReasonCode.ROUNDING_ASSUMPTION_UNSUPPORTED)

    if orderbook.data_quality_state != "VALID":
        reasons.append(ComplementReasonCode.ORDERBOOK_LEVEL_INVALID)
    try:
        yes_asks = _ask_levels(levels, bid_side=OrderbookSide.NO_BID)
        no_asks = _ask_levels(levels, bid_side=OrderbookSide.YES_BID)
    except ValueValidationError:
        yes_asks = ()
        no_asks = ()
        reasons.append(ComplementReasonCode.ORDERBOOK_LEVEL_INVALID)
    if not yes_asks or not no_asks:
        reasons.append(ComplementReasonCode.ORDERBOOK_MISSING_SIDE)

    remaining = maximum
    yes_index = no_index = 0
    yes_used = no_used = Decimal(0)
    slices: list[ComplementFillSlice] = []
    while not reasons and remaining > 0 and yes_index < len(yes_asks) and no_index < len(no_asks):
        yes_level = yes_asks[yes_index]
        no_level = no_asks[no_index]
        quantity = min(
            remaining,
            yes_level.quantity - yes_used,
            no_level.quantity - no_used,
        )
        if quantity <= 0:
            raise ValueValidationError("orderbook matching produced a non-positive slice")
        assert fee is not None and fee.fee_multiplier is not None
        incremental_slices = tuple(
            (
                side,
                level,
                _fill_fee(
                    quantity=quantity,
                    ask_price=level.derived_ask_price,
                    multiplier=fee.fee_multiplier,
                ),
            )
            for side, level in (("YES", yes_level), ("NO", no_level))
        )
        incremental_total_cost = sum(
            (
                quantity * level.derived_ask_price + modeled_fee + rounding_reserve
                for _, level, (modeled_fee, rounding_reserve) in incremental_slices
            ),
            Decimal(0),
        )
        # Each depth extension must retain a positive conservative lower bound.
        # This scanner reports only the executable-quality shadow supported by
        # the bounded book; it never dilutes a prior candidate with a loss-making
        # next slice.
        if quantity - incremental_total_cost <= 0:
            if not slices:
                incremental_acquisition = sum(
                    (quantity * level.derived_ask_price for _, level, _ in incremental_slices),
                    Decimal(0),
                )
                incremental_fees = sum(
                    (trade_fee for _, _, (trade_fee, _) in incremental_slices), Decimal(0)
                )
                incremental_rounding = sum(
                    (rounding for _, _, (_, rounding) in incremental_slices), Decimal(0)
                )
                reasons.append(
                    _non_positive_reason(
                        gross_gap=quantity - incremental_acquisition,
                        modeled_fees=incremental_fees,
                        rounding_reserve=incremental_rounding,
                    )
                )
            break
        for side, level, (trade_fee, rounding_reserve) in incremental_slices:
            acquisition = quantity * level.derived_ask_price
            slice_payload = (
                orderbook.orderbook_snapshot_id,
                side,
                len(slices),
                quantity,
                level.level_id,
            )
            slice_id = f"binary_complement_slice_{content_digest(slice_payload)[:24]}"
            slices.append(
                ComplementFillSlice(
                    slice_id=slice_id,
                    evaluation_id="pending",
                    ordinal=len(slices),
                    purchased_side=side,
                    source_level_id=level.level_id,
                    quantity=quantity,
                    ask_price=level.derived_ask_price,
                    acquisition_cost=acquisition,
                    modeled_trade_fee=trade_fee,
                    rounding_reserve=rounding_reserve,
                )
            )
        remaining -= quantity
        yes_used += quantity
        no_used += quantity
        if yes_used == yes_level.quantity:
            yes_index += 1
            yes_used = Decimal(0)
        if no_used == no_level.quantity:
            no_index += 1
            no_used = Decimal(0)

    matched_quantity = maximum - remaining
    if not reasons and matched_quantity == 0:
        reasons.append(ComplementReasonCode.NO_MATCHED_QUANTITY)
    gross_cost = sum((value.acquisition_cost for value in slices), Decimal(0))
    settlement = matched_quantity * ONE
    trade_fees = sum((value.modeled_trade_fee for value in slices), Decimal(0))
    yes_trade_fees = sum(
        (value.modeled_trade_fee for value in slices if value.purchased_side == "YES"),
        Decimal(0),
    )
    no_trade_fees = trade_fees - yes_trade_fees
    rounding_reserve = sum((value.rounding_reserve for value in slices), Decimal(0))
    total_modeled_acquisition_cost = gross_cost + trade_fees + rounding_reserve
    gross_gap = settlement - gross_cost
    lower_bound = settlement - total_modeled_acquisition_cost
    if not reasons and lower_bound <= 0:
        reasons.append(
            _non_positive_reason(
                gross_gap=gross_gap,
                modeled_fees=trade_fees,
                rounding_reserve=rounding_reserve,
            )
        )

    invalid = ComplementReasonCode.ORDERBOOK_LEVEL_INVALID in reasons
    unsupported = {
        ComplementReasonCode.MARKET_NOT_OPEN,
        ComplementReasonCode.RULE_EVIDENCE_MISSING,
        ComplementReasonCode.SERIES_EVIDENCE_MISSING,
        ComplementReasonCode.ORDERBOOK_MISSING_SIDE,
        ComplementReasonCode.FEE_MODEL_UNSUPPORTED,
        ComplementReasonCode.FEE_EVIDENCE_STALE,
        ComplementReasonCode.ROUNDING_ASSUMPTION_UNSUPPORTED,
    }
    state = (
        ComplementState.INVALID
        if invalid
        else ComplementState.INELIGIBLE
        if set(reasons).intersection(unsupported)
        else ComplementState.NO_CANDIDATE
        if reasons
        else ComplementState.CANDIDATE
    )
    payload = {
        "orderbook_snapshot_id": orderbook.orderbook_snapshot_id,
        "fee_snapshot_id": fee.fee_snapshot_id if fee else None,
        "maximum_bundle_quantity": maximum,
        "evaluator_version": evaluator_version,
        "rounding_identity": ROUNDING_IDENTITY,
        "level_hashes": tuple(value.canonical_hash for value in levels),
        "rule_content_hash": rule_content_hash,
    }
    evaluation_id = f"binary_complement_evaluation_{content_digest(payload)[:24]}"
    completed_slices = tuple(
        ComplementFillSlice(
            slice_id=value.slice_id,
            evaluation_id=evaluation_id,
            ordinal=value.ordinal,
            purchased_side=value.purchased_side,
            source_level_id=value.source_level_id,
            quantity=value.quantity,
            ask_price=value.ask_price,
            acquisition_cost=value.acquisition_cost,
            modeled_trade_fee=value.modeled_trade_fee,
            rounding_reserve=value.rounding_reserve,
        )
        for value in slices
    )
    yes_cost = sum(
        (value.acquisition_cost for value in completed_slices if value.purchased_side == "YES"),
        Decimal(0),
    )
    no_cost = gross_cost - yes_cost
    return (
        BinaryComplementEvaluation(
            evaluation_id=evaluation_id,
            ingestion_run_id=orderbook.ingestion_run_id,
            orderbook_snapshot_id=orderbook.orderbook_snapshot_id,
            market_snapshot_id=orderbook.market_snapshot_id,
            fee_snapshot_id=fee.fee_snapshot_id if fee else None,
            provider=orderbook.provider,
            provider_market_ticker=orderbook.provider_market_ticker,
            evaluator_version=evaluator_version,
            evaluated_at=observed_at,
            maximum_depth=orderbook.maximum_depth,
            maximum_bundle_quantity=maximum,
            rounding_identity=ROUNDING_IDENTITY,
            matched_quantity=matched_quantity,
            weighted_yes_ask=(yes_cost / matched_quantity if matched_quantity else None),
            weighted_no_ask=(no_cost / matched_quantity if matched_quantity else None),
            gross_acquisition_cost=gross_cost,
            gross_settlement_value=settlement,
            gross_complement_gap=gross_gap,
            yes_modeled_taker_fee=yes_trade_fees,
            no_modeled_taker_fee=no_trade_fees,
            modeled_taker_fees=trade_fees,
            conservative_rounding_reserve=rounding_reserve,
            total_modeled_acquisition_cost=total_modeled_acquisition_cost,
            conservative_net_lower_bound=lower_bound,
            net_lower_bound_per_contract=(
                _conservative_per_contract_lower_bound(lower_bound, matched_quantity)
                if matched_quantity
                else None
            ),
            state=state,
            reason_codes=tuple(reasons),
            canonical_input_hash=ContentHash(content_digest(payload)),
        ),
        completed_slices,
    )


def rank_shadow_executable_quality(
    evaluations: tuple[BinaryComplementEvaluation, ...],
) -> tuple[BinaryComplementEvaluation, ...]:
    """Return deterministic display priorities, never an execution recommendation."""

    return tuple(
        sorted(
            (value for value in evaluations if value.state is ComplementState.CANDIDATE),
            key=lambda value: (
                -value.conservative_net_lower_bound,
                -value.matched_quantity,
                -(value.conservative_net_lower_bound / value.matched_quantity),
                -value.gross_complement_gap,
                value.fee_snapshot_id or "",
                _descending_timestamp_key(value.evaluated_at),
                value.provider_market_ticker,
            ),
        )
    )
