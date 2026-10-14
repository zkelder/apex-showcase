"""Provider-neutral normalized portfolio/account truth.

This module owns no provider client, credential, network, execution, or
capital-mutation capability.  It only normalizes already-retained evidence
from the existing per-vertical PAPER account/position records (Crypto Spot
PAPER, Kalshi PAPER, Securities internal PAPER) and the existing genuine
External PAPER (Alpaca) broker-evidence records into one provider-neutral
shape, so later code (in particular ``domain.investment_mandate``) can reason
about "what does Apex currently hold / have available" without depending on
any single vertical's own representation.

Every genuinely unknown fact stays ``None`` (or is listed in
``unavailable_fields``/``unavailable_sources``) rather than being inferred,
defaulted to zero, or silently dropped.  Aggregate totals on
``PortfolioSnapshot`` are conservative-by-construction: a total is populated
only when every contributing figure is genuinely known, and stays ``None``
otherwise -- a partial sum is never presented as if it were complete truth.
Nothing here creates or implies an Order, Position mutation, reservation,
PAPER submission, LIVE authority, or provider write; every persisted fact
this module produces carries ``authority = "NONE"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from apex.domain.campaign import CampaignSnapshot
from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperAccountState,
    CryptoPaperPosition,
)
from apex.domain.external_paper import ExternalPaperBrokerEvidence
from apex.domain.kalshi_operating import KalshiPaperPosition, KalshiPaperState
from apex.domain.securities_paper import SecuritiesPositionState
from apex.domain.values import ValueValidationError, content_digest, deterministic_id, parse_decimal

PORTFOLIO_ACCOUNT_BALANCE_V1 = "PORTFOLIO_ACCOUNT_BALANCE_V1"
PORTFOLIO_POSITION_V1 = "PORTFOLIO_POSITION_V1"
PORTFOLIO_SNAPSHOT_V1 = "PORTFOLIO_SNAPSHOT_V1"
PORTFOLIO_STATE_WARNING = (
    "PORTFOLIO STATE IS NORMALIZED RETAINED EVIDENCE — NOT A LIVE ACCOUNT FEED"
)

# Evidence-class vocabulary, mirroring the "FIXTURE" / "HISTORICAL_PUBLIC" /
# "GENUINE_PUBLIC" convention used elsewhere (e.g. shared_opportunity.py).
EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER = "APEX_SIMULATED_PAPER_LEDGER"
EVIDENCE_CLASS_GENUINE_RETAINED_PROVIDER_EVIDENCE = "GENUINE_RETAINED_PROVIDER_EVIDENCE"
KNOWN_EVIDENCE_CLASSES = frozenset(
    {
        EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        EVIDENCE_CLASS_GENUINE_RETAINED_PROVIDER_EVIDENCE,
    }
)

# Market-family vocabulary.  Kept as plain strings (not a closed StrEnum) on
# the records themselves so a not-yet-enumerated family fails a mandate
# check closed instead of raising -- these constants exist for callers to
# reference the known values without retyping literals.
MARKET_FAMILY_CRYPTO_SPOT = "CRYPTO_SPOT"
MARKET_FAMILY_KALSHI_EVENT_CONTRACT = "KALSHI_EVENT_CONTRACT"
MARKET_FAMILY_US_EQUITY = "US_EQUITY"
MARKET_FAMILY_US_EQUITY_OPTION = "US_EQUITY_OPTION"
KNOWN_MARKET_FAMILIES = frozenset(
    {
        MARKET_FAMILY_CRYPTO_SPOT,
        MARKET_FAMILY_KALSHI_EVENT_CONTRACT,
        MARKET_FAMILY_US_EQUITY,
        MARKET_FAMILY_US_EQUITY_OPTION,
    }
)

_QUANTUM = Decimal("0.00000001")


class PortfolioStateError(ValueError):
    """Raised when retained portfolio evidence is invalid, incomplete, or conflicting."""


class PortfolioVenue(StrEnum):
    """The concrete external venue or internally-managed dataset evidence traces to.

    Deliberately distinct from ``live_readiness.Provider``: it shares that
    enum's string vocabulary for the three genuine external providers
    (``COINBASE``/``KALSHI``/``ALPACA``) so the two can be correlated by
    value, but adds ``APEX_MANAGED_SECURITIES_DATA`` for Securities Stage 1/2
    internal PAPER, which is deliberately backed by Apex's own locally
    managed file evidence and never an Alpaca read -- labeling that exposure
    ``ALPACA`` would misattribute its evidence source.
    """

    COINBASE = "COINBASE"
    KALSHI = "KALSHI"
    ALPACA = "ALPACA"
    APEX_MANAGED_SECURITIES_DATA = "APEX_MANAGED_SECURITIES_DATA"


class AccountKind(StrEnum):
    """How Apex captures/simulates exposure at this venue, independent of what market it is."""

    APEX_INTERNAL_PAPER_SIMULATION = "APEX_INTERNAL_PAPER_SIMULATION"
    PROVIDER_PAPER_BROKER = "PROVIDER_PAPER_BROKER"


class PositionExposure(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PortfolioCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_UNKNOWN_SOURCES = "PARTIAL_UNKNOWN_SOURCES"
    ALL_SOURCES_UNKNOWN = "ALL_SOURCES_UNKNOWN"


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PortfolioStateError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PortfolioStateError(reason)
    return value.astimezone(UTC)


def _decimal(value: object, reason: str, *, nonnegative: bool = False) -> Decimal:
    try:
        parsed = parse_decimal(cast(str | int | Decimal, value), allow_negative=not nonnegative)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise PortfolioStateError(reason) from error
    return parsed


def _optional_decimal(
    value: object | None, reason: str, *, nonnegative: bool = False
) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, reason, nonnegative=nonnegative)


@dataclass(frozen=True, slots=True)
class PortfolioAccountBalance:
    balance_id: str
    venue: PortfolioVenue
    account_kind: AccountKind
    account_id: str
    currency: str
    cash: Decimal | None
    reserved_cash: Decimal | None
    available_cash: Decimal | None
    buying_power: Decimal | None
    equity: Decimal | None
    realized_pnl: Decimal | None
    as_of: datetime
    captured_at: datetime
    evidence_class: str
    source_record_type: str
    source_record_id: str
    source_canonical_hash: str
    unavailable_fields: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        venue: PortfolioVenue,
        account_kind: AccountKind,
        account_id: str,
        currency: str = "USD",
        cash: object | None = None,
        reserved_cash: object | None = None,
        buying_power: object | None = None,
        equity: object | None = None,
        realized_pnl: object | None = None,
        as_of: datetime,
        captured_at: datetime,
        evidence_class: str,
        source_record_type: str,
        source_record_id: str,
        source_canonical_hash: str,
        limitations: tuple[str, ...] = (),
    ) -> PortfolioAccountBalance:
        as_of_time = _time(as_of, "PORTFOLIO_BALANCE_AS_OF_INVALID")
        captured_time = _time(captured_at, "PORTFOLIO_BALANCE_CAPTURED_AT_INVALID")
        if captured_time < as_of_time:
            raise PortfolioStateError("PORTFOLIO_BALANCE_CAPTURED_BEFORE_AS_OF")
        if evidence_class not in KNOWN_EVIDENCE_CLASSES:
            raise PortfolioStateError("PORTFOLIO_BALANCE_EVIDENCE_CLASS_INVALID")
        resolved_cash = _optional_decimal(cash, "PORTFOLIO_BALANCE_CASH_INVALID")
        resolved_reserved = _optional_decimal(
            reserved_cash, "PORTFOLIO_BALANCE_RESERVED_CASH_INVALID", nonnegative=True
        )
        resolved_buying_power = _optional_decimal(
            buying_power, "PORTFOLIO_BALANCE_BUYING_POWER_INVALID"
        )
        resolved_equity = _optional_decimal(equity, "PORTFOLIO_BALANCE_EQUITY_INVALID")
        resolved_realized = _optional_decimal(realized_pnl, "PORTFOLIO_BALANCE_REALIZED_INVALID")
        available_cash = (
            None
            if resolved_cash is None or resolved_reserved is None
            else resolved_cash - resolved_reserved
        )
        unavailable = tuple(
            sorted(
                name
                for name, value in (
                    ("cash", resolved_cash),
                    ("reserved_cash", resolved_reserved),
                    ("available_cash", available_cash),
                    ("buying_power", resolved_buying_power),
                    ("equity", resolved_equity),
                    ("realized_pnl", resolved_realized),
                )
                if value is None
            )
        )
        account = _id(account_id, "PORTFOLIO_BALANCE_ACCOUNT_ID_INVALID")
        values = (
            venue,
            account_kind,
            account,
            _id(currency, "PORTFOLIO_BALANCE_CURRENCY_INVALID", 8),
            resolved_cash,
            resolved_reserved,
            available_cash,
            resolved_buying_power,
            resolved_equity,
            resolved_realized,
            as_of_time,
            captured_time,
            evidence_class,
            _id(source_record_type, "PORTFOLIO_BALANCE_SOURCE_TYPE_INVALID"),
            _id(source_record_id, "PORTFOLIO_BALANCE_SOURCE_ID_INVALID"),
            _id(source_canonical_hash, "PORTFOLIO_BALANCE_SOURCE_HASH_INVALID", 64),
            unavailable,
            tuple(sorted(set(limitations))),
            "NONE",
        )
        balance_id = deterministic_id(
            "portfolio_account_balance",
            (venue, account_kind, account, source_record_type, source_record_id, as_of_time),
        )
        return cls(
            balance_id,
            *values[:-1],
            content_digest((PORTFOLIO_ACCOUNT_BALANCE_V1, balance_id, *values)),
            values[-1],
        )


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    position_id: str
    venue: PortfolioVenue
    account_kind: AccountKind
    account_id: str
    instrument_id: str
    market_family: str
    exposure: PositionExposure
    raw_side: str
    quantity: Decimal | None
    cost_basis: Decimal | None
    mark_value: Decimal | None
    exposure_value: Decimal | None
    exposure_value_source: str | None
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal | None
    opened_at: datetime | None
    as_of: datetime
    captured_at: datetime
    evidence_class: str
    source_record_type: str
    source_record_id: str
    source_canonical_hash: str
    unavailable_fields: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        venue: PortfolioVenue,
        account_kind: AccountKind,
        account_id: str,
        instrument_id: str,
        market_family: str,
        exposure: PositionExposure,
        raw_side: str,
        quantity: object | None,
        cost_basis: object | None = None,
        mark_value: object | None = None,
        unrealized_pnl: object | None = None,
        realized_pnl: object | None = None,
        opened_at: datetime | None,
        as_of: datetime,
        captured_at: datetime,
        evidence_class: str,
        source_record_type: str,
        source_record_id: str,
        source_canonical_hash: str,
        limitations: tuple[str, ...] = (),
    ) -> PortfolioPosition:
        as_of_time = _time(as_of, "PORTFOLIO_POSITION_AS_OF_INVALID")
        captured_time = _time(captured_at, "PORTFOLIO_POSITION_CAPTURED_AT_INVALID")
        if captured_time < as_of_time:
            raise PortfolioStateError("PORTFOLIO_POSITION_CAPTURED_BEFORE_AS_OF")
        opened_time = (
            None if opened_at is None else _time(opened_at, "PORTFOLIO_POSITION_OPENED_AT_INVALID")
        )
        if opened_time is not None and opened_time > as_of_time:
            raise PortfolioStateError("PORTFOLIO_POSITION_OPENED_AFTER_AS_OF")
        if evidence_class not in KNOWN_EVIDENCE_CLASSES:
            raise PortfolioStateError("PORTFOLIO_POSITION_EVIDENCE_CLASS_INVALID")
        resolved_quantity = _optional_decimal(
            quantity, "PORTFOLIO_POSITION_QUANTITY_INVALID", nonnegative=True
        )
        resolved_cost_basis = _optional_decimal(cost_basis, "PORTFOLIO_POSITION_COST_BASIS_INVALID")
        resolved_mark = _optional_decimal(mark_value, "PORTFOLIO_POSITION_MARK_INVALID")
        resolved_unrealized = _optional_decimal(
            unrealized_pnl, "PORTFOLIO_POSITION_UNREALIZED_INVALID"
        )
        resolved_realized = _optional_decimal(realized_pnl, "PORTFOLIO_POSITION_REALIZED_INVALID")
        if resolved_mark is not None:
            exposure_value, exposure_source = resolved_mark, "MARK"
        elif resolved_cost_basis is not None:
            exposure_value, exposure_source = resolved_cost_basis, "COST_BASIS"
        else:
            exposure_value, exposure_source = None, None
        unavailable = tuple(
            sorted(
                name
                for name, value in (
                    ("quantity", resolved_quantity),
                    ("cost_basis", resolved_cost_basis),
                    ("mark_value", resolved_mark),
                    ("exposure_value", exposure_value),
                    ("unrealized_pnl", resolved_unrealized),
                    ("realized_pnl", resolved_realized),
                    ("opened_at", opened_time),
                )
                if value is None
            )
        )
        instrument = _id(instrument_id, "PORTFOLIO_POSITION_INSTRUMENT_INVALID")
        account = _id(account_id, "PORTFOLIO_POSITION_ACCOUNT_ID_INVALID")
        family = _id(market_family, "PORTFOLIO_POSITION_MARKET_FAMILY_INVALID")
        side = _id(raw_side, "PORTFOLIO_POSITION_RAW_SIDE_INVALID", 40)
        values = (
            venue,
            account_kind,
            account,
            instrument,
            family,
            exposure,
            side,
            resolved_quantity,
            resolved_cost_basis,
            resolved_mark,
            exposure_value,
            exposure_source,
            resolved_unrealized,
            resolved_realized,
            opened_time,
            as_of_time,
            captured_time,
            evidence_class,
            _id(source_record_type, "PORTFOLIO_POSITION_SOURCE_TYPE_INVALID"),
            _id(source_record_id, "PORTFOLIO_POSITION_SOURCE_ID_INVALID"),
            _id(source_canonical_hash, "PORTFOLIO_POSITION_SOURCE_HASH_INVALID", 64),
            unavailable,
            tuple(sorted(set(limitations))),
            "NONE",
        )
        position_id = deterministic_id(
            "portfolio_position",
            (venue, account_kind, account, source_record_type, source_record_id, as_of_time),
        )
        return cls(
            position_id,
            *values[:-1],
            content_digest((PORTFOLIO_POSITION_V1, position_id, *values)),
            values[-1],
        )


@dataclass(frozen=True, slots=True)
class UnavailablePortfolioSource:
    venue: PortfolioVenue
    account_kind: AccountKind
    reason: str


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    snapshot_id: str
    version: int
    as_of: datetime
    created_at: datetime
    account_balance_ids: tuple[str, ...]
    position_ids: tuple[str, ...]
    unavailable_sources: tuple[UnavailablePortfolioSource, ...]
    total_known_cash: Decimal | None
    total_known_reserved_cash: Decimal | None
    total_known_position_value: Decimal | None
    total_known_portfolio_value: Decimal | None
    exposure_by_venue: tuple[tuple[str, Decimal], ...]
    exposure_by_market_family: tuple[tuple[str, Decimal], ...]
    exposure_by_instrument: tuple[tuple[str, Decimal], ...]
    positions_with_unknown_exposure_value: tuple[str, ...]
    completeness: PortfolioCompleteness
    source_manifest_hash: str
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def _grouped_known_totals(
    positions: tuple[PortfolioPosition, ...], group_of: Callable[[PortfolioPosition], str]
) -> tuple[tuple[str, Decimal], ...]:
    totals: dict[str, Decimal] = {}
    for position in positions:
        if position.exposure_value is None:
            continue
        group = group_of(position)
        totals[group] = totals.get(group, Decimal(0)) + position.exposure_value
    return tuple(sorted(totals.items()))


def build_portfolio_snapshot(
    *,
    snapshot_id: str,
    as_of: datetime,
    created_at: datetime,
    account_balances: tuple[PortfolioAccountBalance, ...],
    positions: tuple[PortfolioPosition, ...],
    unavailable_sources: tuple[UnavailablePortfolioSource, ...],
) -> PortfolioSnapshot:
    as_of_time = _time(as_of, "PORTFOLIO_SNAPSHOT_AS_OF_INVALID")
    created_time = _time(created_at, "PORTFOLIO_SNAPSHOT_CREATED_AT_INVALID")
    if created_time < as_of_time:
        raise PortfolioStateError("PORTFOLIO_SNAPSHOT_CREATED_BEFORE_AS_OF")
    if any(item.as_of > as_of_time for item in account_balances) or any(
        item.as_of > as_of_time for item in positions
    ):
        raise PortfolioStateError("PORTFOLIO_SNAPSHOT_CONTAINS_FUTURE_EVIDENCE")
    ordered_balances = tuple(sorted(account_balances, key=lambda item: item.balance_id))
    ordered_positions = tuple(sorted(positions, key=lambda item: item.position_id))
    if len({item.balance_id for item in ordered_balances}) != len(ordered_balances):
        raise PortfolioStateError("PORTFOLIO_SNAPSHOT_DUPLICATE_BALANCE_ID")
    if len({item.position_id for item in ordered_positions}) != len(ordered_positions):
        raise PortfolioStateError("PORTFOLIO_SNAPSHOT_DUPLICATE_POSITION_ID")
    ordered_unavailable = tuple(
        sorted(unavailable_sources, key=lambda item: (item.venue.value, item.account_kind.value))
    )

    any_cash_unknown = any(item.cash is None for item in ordered_balances)
    any_reserved_unknown = any(item.reserved_cash is None for item in ordered_balances)
    any_position_value_unknown = any(item.exposure_value is None for item in ordered_positions)
    sources_incomplete = bool(ordered_unavailable)

    total_known_cash = (
        None
        if any_cash_unknown or sources_incomplete
        else sum((item.cash or Decimal(0) for item in ordered_balances), Decimal(0))
    )
    total_known_reserved_cash = (
        None
        if any_reserved_unknown or sources_incomplete
        else sum((item.reserved_cash or Decimal(0) for item in ordered_balances), Decimal(0))
    )
    total_known_position_value = (
        None
        if any_position_value_unknown or sources_incomplete
        else sum((item.exposure_value or Decimal(0) for item in ordered_positions), Decimal(0))
    )
    total_known_portfolio_value = (
        None
        if total_known_cash is None or total_known_position_value is None
        else total_known_cash + total_known_position_value
    )

    if not ordered_unavailable:
        completeness = PortfolioCompleteness.COMPLETE
    elif ordered_balances or ordered_positions:
        completeness = PortfolioCompleteness.PARTIAL_UNKNOWN_SOURCES
    else:
        completeness = PortfolioCompleteness.ALL_SOURCES_UNKNOWN

    manifest = content_digest(
        (
            tuple((item.balance_id, item.canonical_hash) for item in ordered_balances),
            tuple((item.position_id, item.canonical_hash) for item in ordered_positions),
            tuple((item.venue, item.account_kind, item.reason) for item in ordered_unavailable),
        )
    )
    warnings = (PORTFOLIO_STATE_WARNING,)
    limitations = (
        "APEX_INTERNAL_PAPER_BALANCES_ARE_SIMULATED_NOT_GENUINE_PROVIDER_TRUTH",
        "AGGREGATE_TOTALS_ARE_NULL_UNLESS_EVERY_CONTRIBUTING_FIGURE_IS_KNOWN",
        "MARK_VALUES_REQUIRE_A_SEPARATELY_SUPPLIED_RECENT_MARK_AND_MAY_BE_ABSENT",
    )
    values = (
        1,
        as_of_time,
        created_time,
        tuple(item.balance_id for item in ordered_balances),
        tuple(item.position_id for item in ordered_positions),
        ordered_unavailable,
        total_known_cash,
        total_known_reserved_cash,
        total_known_position_value,
        total_known_portfolio_value,
        _grouped_known_totals(ordered_positions, lambda item: item.venue.value),
        _grouped_known_totals(ordered_positions, lambda item: item.market_family),
        _grouped_known_totals(ordered_positions, lambda item: item.instrument_id),
        tuple(item.position_id for item in ordered_positions if item.exposure_value is None),
        completeness,
        manifest,
        warnings,
        limitations,
        "NONE",
    )
    identity = _id(snapshot_id, "PORTFOLIO_SNAPSHOT_ID_INVALID")
    return PortfolioSnapshot(
        identity,
        *values[:-1],
        content_digest((PORTFOLIO_SNAPSHOT_V1, identity, *values)),
        values[-1],
    )


# --------------------------------------------------------------------------
# Pure mappings from each vertical's own already-retained evidence into the
# normalized shapes above.  Each function takes already-fetched domain
# objects only (no I/O, no provider call) and returns ``None`` only when the
# source genuinely lacks enough evidence to construct a normalized record at
# all (e.g. an External PAPER field the provider did not return) -- callers
# are responsible for recording an ``UnavailablePortfolioSource`` in that
# case rather than inferring a value.
# --------------------------------------------------------------------------


def balance_from_crypto_paper(
    *,
    account: CryptoPaperAccount,
    state: CryptoPaperAccountState,
    as_of: datetime,
    captured_at: datetime,
) -> PortfolioAccountBalance:
    return PortfolioAccountBalance.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=account.account_id,
        cash=state.cash,
        reserved_cash=state.reserved_cash,
        realized_pnl=state.realized_paper_pnl,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="CryptoPaperAccountState",
        source_record_id=f"{state.account_id}:{state.version}",
        source_canonical_hash=state.canonical_hash,
        limitations=(
            "APEX_INTERNAL_SIMULATION_NOT_GENUINE_PROVIDER_BALANCE",
            "EQUITY_AND_BUYING_POWER_REQUIRE_A_LIVE_MARK_NOT_COMPUTED_HERE",
        ),
    )


def position_from_crypto_paper(
    *, position: CryptoPaperPosition, as_of: datetime, captured_at: datetime
) -> PortfolioPosition:
    return PortfolioPosition.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=position.account_id,
        instrument_id=position.product_id,
        market_family=MARKET_FAMILY_CRYPTO_SPOT,
        exposure=PositionExposure.LONG,
        raw_side="SPOT_LONG",
        quantity=position.base_quantity,
        cost_basis=position.cumulative_cost,
        realized_pnl=position.realized_paper_pnl,
        opened_at=None,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="CryptoPaperPosition",
        source_record_id=f"{position.position_id}:{position.version}",
        source_canonical_hash=position.canonical_hash,
        limitations=(
            "APEX_INTERNAL_SIMULATION_NOT_GENUINE_PROVIDER_POSITION",
            "MARK_VALUE_REQUIRES_A_SEPARATELY_SUPPLIED_RECENT_MARK",
            "OPENED_AT_NOT_SEPARATELY_TRACKED_ON_THIS_RECORD",
        ),
    )


def balance_from_kalshi_paper(
    *, state: KalshiPaperState, as_of: datetime, captured_at: datetime
) -> PortfolioAccountBalance:
    record_id = (
        state.account.account_id
        if state.updated_at is None
        else f"{state.account.account_id}:{state.updated_at.isoformat()}"
    )
    return PortfolioAccountBalance.create(
        venue=PortfolioVenue.KALSHI,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=state.account.account_id,
        cash=state.cash,
        reserved_cash=state.reserved_cash,
        realized_pnl=state.realized_paper_pnl,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="KalshiPaperState",
        source_record_id=record_id,
        source_canonical_hash=state.canonical_hash,
        limitations=("APEX_INTERNAL_SIMULATION_NOT_GENUINE_PROVIDER_BALANCE",),
    )


def position_from_kalshi_paper(
    *,
    position: KalshiPaperPosition,
    account_id: str,
    as_of: datetime,
    captured_at: datetime,
) -> PortfolioPosition:
    return PortfolioPosition.create(
        venue=PortfolioVenue.KALSHI,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=account_id,
        instrument_id=position.market_ticker,
        market_family=MARKET_FAMILY_KALSHI_EVENT_CONTRACT,
        exposure=PositionExposure.LONG,
        raw_side=position.side.value,
        quantity=Decimal(position.contracts),
        cost_basis=position.cost_basis,
        opened_at=position.opened_at,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="KalshiPaperPosition",
        source_record_id=position.position_id,
        source_canonical_hash=position.canonical_hash,
        limitations=(
            "APEX_INTERNAL_SIMULATION_NOT_GENUINE_PROVIDER_POSITION",
            "MARK_VALUE_REQUIRES_A_SEPARATELY_SUPPLIED_RECENT_MARK",
            "PER_POSITION_REALIZED_PNL_NOT_SEPARATELY_TRACKED",
        ),
    )


def balance_from_securities_campaign(
    *,
    campaign_id: str,
    snapshot: CampaignSnapshot,
    as_of: datetime,
    captured_at: datetime,
) -> PortfolioAccountBalance:
    return PortfolioAccountBalance.create(
        venue=PortfolioVenue.APEX_MANAGED_SECURITIES_DATA,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=campaign_id,
        cash=snapshot.settled_cash.amount,
        reserved_cash=snapshot.active_reservations.amount,
        equity=snapshot.ledger_equity.amount,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="CampaignSnapshot",
        source_record_id=campaign_id,
        source_canonical_hash=content_digest(snapshot),
        limitations=(
            "APEX_INTERNAL_SIMULATION_NOT_GENUINE_PROVIDER_BALANCE",
            "REALIZED_PNL_NOT_SEPARATELY_TRACKED_AT_CAMPAIGN_LEVEL",
        ),
    )


def position_from_securities_paper(
    *, position: SecuritiesPositionState, as_of: datetime, captured_at: datetime
) -> PortfolioPosition:
    mark_value = (
        None
        if position.last_mark_price is None
        else position.last_mark_price * position.open_quantity.value
    )
    return PortfolioPosition.create(
        venue=PortfolioVenue.APEX_MANAGED_SECURITIES_DATA,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=position.campaign_id,
        instrument_id=position.instrument_id,
        market_family=MARKET_FAMILY_US_EQUITY,
        exposure=PositionExposure.LONG,
        raw_side="LONG",
        quantity=position.open_quantity.value,
        cost_basis=position.average_cost * position.open_quantity.value,
        mark_value=mark_value,
        unrealized_pnl=position.unrealized_pnl,
        realized_pnl=position.realized_pnl,
        opened_at=None,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="SecuritiesPositionState",
        source_record_id=f"{position.position_id}:{position.row_version}",
        source_canonical_hash=position.canonical_hash.value,
        limitations=(
            "APEX_INTERNAL_SIMULATION_NOT_GENUINE_PROVIDER_POSITION",
            "OPENED_AT_NOT_SEPARATELY_TRACKED_ON_THIS_RECORD",
        ),
    )


def _external_paper_decimal(payload: dict[str, str | None], key: str) -> Decimal | None:
    raw = payload.get(key)
    if raw is None:
        return None
    try:
        return parse_decimal(raw, allow_negative=True)
    except ValueValidationError:
        return None


def balance_from_external_paper_evidence(
    evidence: ExternalPaperBrokerEvidence,
    *,
    account_id: str,
    as_of: datetime,
    captured_at: datetime,
) -> PortfolioAccountBalance | None:
    """Normalize a genuine, already-retained Alpaca ``/v2/account`` read.

    Returns ``None`` when the sanitized payload carries none of the fields
    this record needs -- the caller should then record an
    ``UnavailablePortfolioSource`` rather than fabricate a balance.
    """

    payload = evidence.sanitized_payload
    cash = _external_paper_decimal(payload, "cash")
    buying_power = _external_paper_decimal(payload, "buying_power")
    equity = _external_paper_decimal(payload, "equity")
    if cash is None and buying_power is None and equity is None:
        return None
    limitations = ["GENUINE_PROVIDER_EVIDENCE_SANITIZED_FIELD_SUBSET_ONLY"]
    status = payload.get("status")
    if status is not None and status != "ACTIVE":
        limitations.append(f"PROVIDER_ACCOUNT_STATUS:{status}")
    return PortfolioAccountBalance.create(
        venue=PortfolioVenue.ALPACA,
        account_kind=AccountKind.PROVIDER_PAPER_BROKER,
        account_id=account_id,
        cash=cash,
        buying_power=buying_power,
        equity=equity,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_GENUINE_RETAINED_PROVIDER_EVIDENCE,
        source_record_type="ExternalPaperBrokerEvidence",
        source_record_id=evidence.evidence_id,
        source_canonical_hash=evidence.canonical_hash,
        limitations=tuple(limitations),
    )


def position_from_external_paper_evidence(
    evidence: ExternalPaperBrokerEvidence,
    *,
    account_id: str,
    as_of: datetime,
    captured_at: datetime,
) -> PortfolioPosition | None:
    """Normalize one genuine, already-retained Alpaca ``/v2/positions`` item.

    Returns ``None`` when the sanitized payload has no symbol at all --
    the caller should then record an ``UnavailablePortfolioSource`` rather
    than fabricate a position.
    """

    payload = evidence.sanitized_payload
    symbol = payload.get("symbol")
    if not symbol:
        return None
    quantity = _external_paper_decimal(payload, "qty")
    side_raw = payload.get("side") or "long"
    exposure = PositionExposure.SHORT if side_raw.lower() == "short" else PositionExposure.LONG
    return PortfolioPosition.create(
        venue=PortfolioVenue.ALPACA,
        account_kind=AccountKind.PROVIDER_PAPER_BROKER,
        account_id=account_id,
        instrument_id=symbol,
        market_family=MARKET_FAMILY_US_EQUITY,
        exposure=exposure,
        raw_side=side_raw,
        quantity=None if quantity is None else abs(quantity),
        cost_basis=_external_paper_decimal(payload, "cost_basis"),
        mark_value=_external_paper_decimal(payload, "market_value"),
        opened_at=None,
        as_of=as_of,
        captured_at=captured_at,
        evidence_class=EVIDENCE_CLASS_GENUINE_RETAINED_PROVIDER_EVIDENCE,
        source_record_type="ExternalPaperBrokerEvidence",
        source_record_id=evidence.evidence_id,
        source_canonical_hash=evidence.canonical_hash,
        limitations=(
            "GENUINE_PROVIDER_EVIDENCE_SANITIZED_FIELD_SUBSET_ONLY",
            "OPENED_AT_NOT_RETAINED_ON_THIS_EVIDENCE",
        ),
    )
