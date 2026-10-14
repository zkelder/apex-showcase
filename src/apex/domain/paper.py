"""Exact, bounded PAPER execution and marking contracts.

These values model retained evidence only.  They neither contact a provider nor
grant execution authority outside the Paper Portfolio application service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.decisions import Side
from apex.domain.values import (
    ContentHash,
    Money,
    Price,
    Quantity,
    RiskFraction,
    ValueValidationError,
    content_digest,
    floor_to_increment,
    parse_decimal,
)


class PaperExecutionModel(StrEnum):
    IMMEDIATE_CROSS_V1 = "PAPER_IMMEDIATE_CROSS_V1"


class PaperUnfilledBehavior(StrEnum):
    EXPIRE = "EXPIRE"
    CANCEL = "CANCEL"


class PaperFillState(StrEnum):
    NO_FILL = "NO_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PaperExecutionPolicy:
    policy_version_id: str
    execution_model: PaperExecutionModel
    maximum_evidence_age_seconds: int
    maximum_spread: Decimal
    maximum_orderbook_depth: int
    maximum_visible_size_participation: RiskFraction
    slippage_reserve_per_contract: Money
    fee_model_version: str
    time_in_force_seconds: int
    unfilled_behavior: PaperUnfilledBehavior
    configuration_version: str
    configuration_hash: ContentHash

    def __post_init__(self) -> None:
        if (
            not self.policy_version_id
            or not self.fee_model_version
            or not self.configuration_version
        ):
            raise ValueValidationError("paper execution policy requires bounded identities")
        if not 0 <= self.maximum_evidence_age_seconds <= 3_600:
            raise ValueValidationError("paper evidence age must be between zero and one hour")
        spread = parse_decimal(self.maximum_spread)
        if not 0 <= spread <= 1:
            raise ValueValidationError("paper maximum spread must be in [0, 1]")
        if not 1 <= self.maximum_orderbook_depth <= 100:
            raise ValueValidationError("paper orderbook depth must be between 1 and 100")
        if self.maximum_visible_size_participation.value <= 0:
            raise ValueValidationError("paper visible-size participation must be positive")
        if self.slippage_reserve_per_contract.amount < 0:
            raise ValueValidationError("paper slippage reserve cannot be negative")
        if not 1 <= self.time_in_force_seconds <= 86_400:
            raise ValueValidationError("paper time in force must be bounded")
        if self.unfilled_behavior is not PaperUnfilledBehavior.EXPIRE:
            raise ValueValidationError("initial paper execution supports expiration only")
        object.__setattr__(self, "maximum_spread", spread)


@dataclass(frozen=True, slots=True)
class PaperOrderbookLevel:
    price: Price
    quantity: Quantity

    def __post_init__(self) -> None:
        if self.quantity.value <= 0:
            raise ValueValidationError("paper orderbook level quantity must be positive")


@dataclass(frozen=True, slots=True)
class PaperExecutionEvidence:
    evidence_id: str
    provider: str
    provider_market_ticker: str
    provider_series_ticker: str
    market_observation_source_id: str
    instrument_id: str
    rule_version_id: str
    rule_content_hash: ContentHash
    fee_model_version: str
    observed_at: datetime
    available_at: datetime
    yes_asks: tuple[PaperOrderbookLevel, ...]
    no_asks: tuple[PaperOrderbookLevel, ...]
    yes_bids: tuple[PaperOrderbookLevel, ...]
    no_bids: tuple[PaperOrderbookLevel, ...]
    evidence_references: tuple[str, ...]
    content_hash: ContentHash

    def __post_init__(self) -> None:
        if not all(
            (
                self.evidence_id,
                self.provider,
                self.provider_market_ticker,
                self.provider_series_ticker,
                self.market_observation_source_id,
                self.instrument_id,
                self.rule_version_id,
                self.fee_model_version,
            )
        ):
            raise ValueValidationError("paper evidence requires exact market lineage")
        for label, value in (("observed", self.observed_at), ("available", self.available_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueValidationError(f"paper evidence {label} time must be timezone-aware")
        observed = self.observed_at.astimezone(UTC)
        available = self.available_at.astimezone(UTC)
        if observed > available:
            raise ValueValidationError("paper evidence cannot be available before observation")
        if (
            not self.evidence_references
            or tuple(sorted(set(self.evidence_references))) != self.evidence_references
        ):
            raise ValueValidationError(
                "paper evidence references must be deterministic and non-empty"
            )
        for ladder in (self.yes_asks, self.no_asks):
            if tuple(level.price.value for level in ladder) != tuple(
                sorted(level.price.value for level in ladder)
            ):
                raise ValueValidationError("paper ask ladders must be ascending")
        for ladder in (self.yes_bids, self.no_bids):
            if tuple(level.price.value for level in ladder) != tuple(
                sorted((level.price.value for level in ladder), reverse=True)
            ):
                raise ValueValidationError("paper bid ladders must be descending")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)

    def asks_for(self, side: Side) -> tuple[PaperOrderbookLevel, ...]:
        return self.yes_asks if side is Side.YES else self.no_asks

    def bids_for(self, side: Side) -> tuple[PaperOrderbookLevel, ...]:
        return self.yes_bids if side is Side.YES else self.no_bids


@dataclass(frozen=True, slots=True)
class PaperFillEvaluation:
    state: PaperFillState
    filled_quantity: Quantity
    unfilled_quantity: Quantity
    volume_weighted_price: Price | None
    consideration: Money
    fee: Money
    slippage_reserve: Money
    modeled_total_acquisition_cost: Money
    filled_maximum_loss: Money
    remaining_reservation_risk: Money
    reason_codes: tuple[str, ...]
    input_hash: str
    result_hash: str


def evaluate_immediate_cross(
    *,
    policy: PaperExecutionPolicy,
    evidence: PaperExecutionEvidence,
    side: Side,
    requested_quantity: Quantity,
    limit_price: Price,
    maximum_loss_per_unit: Money,
    fee_per_unit: Money,
    cycle_at: datetime,
) -> PaperFillEvaluation:
    """Match only retained displayed asks; no midpoint or hidden liquidity exists."""

    if cycle_at.tzinfo is None or cycle_at.utcoffset() is None:
        raise ValueValidationError("paper cycle time must be timezone-aware")
    now = cycle_at.astimezone(UTC)
    payload = {
        "policy": policy,
        "evidence": evidence,
        "side": side,
        "requested_quantity": requested_quantity,
        "limit_price": limit_price,
        "maximum_loss_per_unit": maximum_loss_per_unit,
        "fee_per_unit": fee_per_unit,
        "cycle_at": now,
    }
    reasons: list[str] = []
    if policy.execution_model is not PaperExecutionModel.IMMEDIATE_CROSS_V1:
        reasons.append("PAPER_EXECUTION_MODEL_UNSUPPORTED")
    if evidence.fee_model_version != policy.fee_model_version:
        reasons.append("PAPER_FEE_MODEL_MISMATCH")
    if (
        now < evidence.available_at
        or (now - evidence.available_at).total_seconds() > policy.maximum_evidence_age_seconds
    ):
        reasons.append("PAPER_EVIDENCE_STALE")
    ladder = evidence.asks_for(side)[: policy.maximum_orderbook_depth]
    if not ladder:
        reasons.append("PAPER_ORDERBOOK_MISSING_ASK")
    if ladder and ladder[0].price.tick != limit_price.tick:
        reasons.append("PAPER_PRICE_TICK_MISMATCH")
    if ladder and ladder[0].price.value > limit_price.value:
        reasons.append("PAPER_LIMIT_NOT_CROSSING")
    bids = evidence.bids_for(side)
    if not bids:
        reasons.append("PAPER_SPREAD_UNAVAILABLE")
    elif bids[0].price.tick != limit_price.tick:
        reasons.append("PAPER_PRICE_TICK_MISMATCH")
    elif ladder and ladder[0].price.value - bids[0].price.value > policy.maximum_spread:
        reasons.append("PAPER_SPREAD_EXCEEDS_POLICY")
    if reasons:
        quantity = Quantity(0, requested_quantity.increment)
        return PaperFillEvaluation(
            state=PaperFillState.REJECTED,
            filled_quantity=quantity,
            unfilled_quantity=requested_quantity,
            volume_weighted_price=None,
            consideration=Money(0),
            fee=Money(0),
            slippage_reserve=Money(0),
            modeled_total_acquisition_cost=Money(0),
            filled_maximum_loss=Money(0),
            remaining_reservation_risk=Money(
                maximum_loss_per_unit.amount * requested_quantity.value
            ),
            reason_codes=tuple(sorted(reasons)),
            input_hash=content_digest(payload),
            result_hash=content_digest({"payload": payload, "reasons": tuple(sorted(reasons))}),
        )
    visible = sum(
        (level.quantity.value for level in ladder if level.price.value <= limit_price.value),
        Decimal(0),
    )
    capped = min(
        requested_quantity.value, visible * policy.maximum_visible_size_participation.value
    )
    filled = floor_to_increment(capped, requested_quantity.increment)
    spent = Decimal(0)
    remaining = filled
    for level in ladder:
        if level.price.value > limit_price.value or remaining <= 0:
            break
        taken = min(remaining, level.quantity.value)
        spent += taken * level.price.value
        remaining -= taken
    if remaining != 0:
        raise ValueValidationError("paper displayed-depth match is inconsistent")
    filled_quantity = Quantity(filled, requested_quantity.increment)
    unfilled_quantity = Quantity(requested_quantity.value - filled, requested_quantity.increment)
    if filled == 0:
        state = PaperFillState.NO_FILL
        price = None
    else:
        state = (
            PaperFillState.FULL_FILL
            if filled == requested_quantity.value
            else PaperFillState.PARTIAL_FILL
        )
        volume_weighted_value = spent / filled
        # One durable PAPER fill has one price, and v0.1 deliberately forbids
        # generalized multi-fill execution.  Never round an average into an
        # invented fill price.
        if volume_weighted_value % limit_price.tick != 0:
            return PaperFillEvaluation(
                state=PaperFillState.REJECTED,
                filled_quantity=Quantity(0, requested_quantity.increment),
                unfilled_quantity=requested_quantity,
                volume_weighted_price=None,
                consideration=Money(0),
                fee=Money(0),
                slippage_reserve=Money(0),
                modeled_total_acquisition_cost=Money(0),
                filled_maximum_loss=Money(0),
                remaining_reservation_risk=Money(
                    maximum_loss_per_unit.amount * requested_quantity.value
                ),
                reason_codes=("PAPER_VWAP_TICK_UNREPRESENTABLE",),
                input_hash=content_digest(payload),
                result_hash=content_digest(
                    {"payload": payload, "reasons": ("PAPER_VWAP_TICK_UNREPRESENTABLE",)}
                ),
            )
        price = Price(volume_weighted_value, limit_price.tick)
    fee = Money(fee_per_unit.amount * filled)
    slippage = Money(policy.slippage_reserve_per_contract.amount * filled)
    consideration = Money(spent)
    result_payload = {
        "payload": payload,
        "state": state,
        "filled_quantity": filled_quantity,
        "unfilled_quantity": unfilled_quantity,
        "volume_weighted_price": price,
        "consideration": consideration,
        "fee": fee,
        "slippage_reserve": slippage,
        "reason_codes": ("PAPER_DISPLAYED_DEPTH_MATCHED",)
        if filled
        else ("PAPER_NO_SUPPORTED_LIQUIDITY",),
    }
    return PaperFillEvaluation(
        state=state,
        filled_quantity=filled_quantity,
        unfilled_quantity=unfilled_quantity,
        volume_weighted_price=price,
        consideration=consideration,
        fee=fee,
        slippage_reserve=slippage,
        modeled_total_acquisition_cost=Money(spent + fee.amount + slippage.amount),
        filled_maximum_loss=Money(maximum_loss_per_unit.amount * filled),
        remaining_reservation_risk=Money(maximum_loss_per_unit.amount * unfilled_quantity.value),
        reason_codes=("PAPER_DISPLAYED_DEPTH_MATCHED",)
        if filled
        else ("PAPER_NO_SUPPORTED_LIQUIDITY",),
        input_hash=content_digest(payload),
        result_hash=content_digest(result_payload),
    )


@dataclass(frozen=True, slots=True)
class PaperPositionMark:
    mark_id: str
    position_id: str
    observed_at: datetime
    mark_model_version: str
    supported_quantity: Quantity
    unsupported_quantity: Quantity
    retained_position_cost: Money
    conservative_liquidation_value: Money
    estimated_close_fee: Money
    slippage_reserve: Money
    informational_midpoint_value: Money | None
    reason_codes: tuple[str, ...]
    input_hash: ContentHash
    result_hash: ContentHash

    @property
    def conservative_unrealized_pnl(self) -> Decimal:
        return (
            self.conservative_liquidation_value.amount
            - self.retained_position_cost.amount
            - self.estimated_close_fee.amount
            - self.slippage_reserve.amount
        )


def evaluate_conservative_mark(
    *,
    mark_id: str,
    position_id: str,
    side: Side,
    quantity: Quantity,
    retained_position_cost: Money,
    evidence: PaperExecutionEvidence,
    maximum_depth: int,
    close_fee_per_unit: Money,
    slippage_reserve_per_contract: Money,
    marked_at: datetime,
) -> PaperPositionMark:
    """Value only visible executable bids; unshown depth remains unsupported."""

    if marked_at.tzinfo is None or marked_at.utcoffset() is None:
        raise ValueValidationError("paper mark time must be timezone-aware")
    if not mark_id or not position_id or not 1 <= maximum_depth <= 100:
        raise ValueValidationError("paper mark requires bounded identity and depth")
    ladder = evidence.bids_for(side)[:maximum_depth]
    supported = Decimal(0)
    proceeds = Decimal(0)
    remaining = quantity.value
    for level in ladder:
        if remaining <= 0:
            break
        taken = min(remaining, level.quantity.value)
        supported += taken
        proceeds += taken * level.price.value
        remaining -= taken
    supported_quantity = Quantity(supported, quantity.increment)
    unsupported_quantity = Quantity(quantity.value - supported, quantity.increment)
    close_fee = Money(close_fee_per_unit.amount * supported)
    slippage = Money(slippage_reserve_per_contract.amount * supported)
    asks = evidence.asks_for(side)
    midpoint = (
        Money((ladder[0].price.value + asks[0].price.value) * quantity.value / Decimal(2))
        if ladder and asks
        else None
    )
    reasons = (
        ("PAPER_MARK_VISIBLE_BID_DEPTH",)
        if supported == quantity.value
        else ("PAPER_MARK_UNSUPPORTED_REMAINDER",)
    )
    payload = {
        "mark_id": mark_id,
        "position_id": position_id,
        "side": side,
        "quantity": quantity,
        "retained_position_cost": retained_position_cost,
        "evidence": evidence,
        "maximum_depth": maximum_depth,
        "close_fee_per_unit": close_fee_per_unit,
        "slippage_reserve_per_contract": slippage_reserve_per_contract,
    }
    return PaperPositionMark(
        mark_id=mark_id,
        position_id=position_id,
        observed_at=evidence.observed_at,
        mark_model_version="PAPER_CONSERVATIVE_LIQUIDATION_V1",
        supported_quantity=supported_quantity,
        unsupported_quantity=unsupported_quantity,
        retained_position_cost=retained_position_cost,
        conservative_liquidation_value=Money(proceeds),
        estimated_close_fee=close_fee,
        slippage_reserve=slippage,
        informational_midpoint_value=midpoint,
        reason_codes=reasons,
        input_hash=ContentHash(content_digest(payload)),
        result_hash=ContentHash(
            content_digest(
                {
                    "payload": payload,
                    "supported_quantity": supported_quantity,
                    "conservative_liquidation_value": Money(proceeds),
                    "estimated_close_fee": close_fee,
                    "slippage_reserve": slippage,
                    "reason_codes": reasons,
                }
            )
        ),
    )
