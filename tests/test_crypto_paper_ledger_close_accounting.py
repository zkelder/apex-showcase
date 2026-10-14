"""Crypto PAPER Ledger Close Accounting Integrity v1.

Production-shaped reproduction of the generation-1 strategy PAPER trial's
ledger (production account ``genuine-strategy-crypto-paper-account``,
BUY 2026-08-30T00:04:30Z, SELL 2026-09-30T00:04:09Z) using the exact
retained fill economics:

* BUY  0.00012687 BTC @ 78293.22 (evidence 78215.00 + 10 bps) -> quote 9.93,
  fee 0.05 (60 bps, floored), cash out 9.98
* SELL 0.00012687 BTC @ 83554.78 (evidence 83638.42 - 10 bps) -> quote 10.60,
  fee 0.06, net proceeds 10.54

The PAPER ledger contract (see ``domain.crypto_paper`` "Ledger contract"):
``PAPER_CRYPTO_ASSET_COST`` carries fee-exclusive consideration,
``PAPER_MODELED_FEE_EXPENSE`` carries every BUY and SELL fee exactly once,
``PAPER_REALIZED_PNL`` carries gross trading P&L (sale consideration minus
relieved consideration). For this trial: asset cost 0, gross P&L 0.67,
fees 0.11, net 0.56 -- equal to the account/position/closure result.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pytest

from apex.application.crypto_paper import (
    CorrectCryptoPaperCloseAccounting,
    InMemoryCryptoPaperRepository,
    ReconcileCryptoPaperAccount,
)
from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperAccountState,
    CryptoPaperError,
    CryptoPaperFill,
    CryptoPaperFillApplication,
    CryptoPaperJournal,
    CryptoPaperOperation,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperProviderContract,
    CryptoPaperReservation,
    CryptoPaperState,
    PaperOrderState,
    PaperOrderType,
    PaperPricingSource,
    PaperReconciliationStatus,
    PaperReservationState,
    PaperSide,
    RetainedPaperPriceEvidence,
    apply_retained_evidence,
    build_close_accounting_correction,
    build_provider_contract,
    ledger_contract_discrepancies,
    reconcile_paper_state,
)
from apex.domain.crypto_universe import PublicCryptoProduct
from apex.domain.research_evidence import EvidenceClass

ACCOUNT_ID = "genuine-strategy-crypto-paper-account"
BUY_AT = datetime(2026, 8, 30, 0, 0, 30, tzinfo=UTC)
SELL_AT = datetime(2026, 9, 30, 0, 0, 3, tzinfo=UTC)
BASE = Decimal("0.00012687")
BUY_EVIDENCE_PRICE = Decimal("78215.00")
SELL_EVIDENCE_PRICE = Decimal("83638.42")
FEE_BPS = "60"
SLIPPAGE_BPS = "10"
CENT = Decimal("0.01")
SATOSHI = Decimal("0.00000001")


def _product() -> PublicCryptoProduct:
    return PublicCryptoProduct.create(
        product_id="BTC-USD",
        provider="COINBASE_ADVANCED_TRADE",
        base_asset="BTC",
        quote_asset="USD",
        market_type="SPOT",
        trading_status="ACTIVE",
        product_status="ONLINE",
        price_increment="0.01",
        quantity_increment="0.00000001",
        minimum_quantity="0.00000001",
        minimum_notional="1",
        public_volume_24h="1000000",
        captured_at=BUY_AT - timedelta(hours=1),
        source_contract_version="COINBASE_PUBLIC_PRODUCTS_CURSOR_V1",
        raw_evidence_hash="a" * 64,
        parser_version="CRYPTO_PRODUCT_PARSER_V1",
        evidence_class=EvidenceClass.FIXTURE,
    )


CONTRACT = build_provider_contract(
    contract_id="coinbase-btc-usd-paper-contract-v1",
    product=_product(),
    source_capture_id="fixture-capture",
)
ACCOUNT = CryptoPaperAccount.create(
    account_id=ACCOUNT_ID,
    opening_cash="1000",
    created_at=BUY_AT - timedelta(days=30),
)


class Book:
    """A minimal, append-only PAPER book driven only through the real,
    unmodified ``apply_retained_evidence`` -- orders/reservations are the
    exact OPEN/HELD shapes ``submit_paper_order`` produces."""

    def __init__(self, contract: CryptoPaperProviderContract = CONTRACT) -> None:
        self.contract = contract
        self.account_state = CryptoPaperAccountState.opening(account=ACCOUNT, event_id="created")
        self.orders: list[CryptoPaperOrder] = []
        self.reservations: list[CryptoPaperReservation] = []
        self.fills: list[CryptoPaperFill] = []
        self.positions: dict[str, CryptoPaperPosition] = {}
        self.journals: list[CryptoPaperJournal] = []
        self._sequence = 0

    def position(self, product_id: str = "BTC-USD") -> CryptoPaperPosition | None:
        return self.positions.get(product_id)

    def trade(
        self,
        side: PaperSide,
        base: Decimal,
        evidence_price: Decimal,
        at: datetime,
        *,
        fills: tuple[Decimal, ...] | None = None,
        product_id: str = "BTC-USD",
        order_id: str | None = None,
        fill_id: str | None = None,
        evidence_id: str | None = None,
        evidence_at: datetime | None = None,
    ) -> None:
        """Submit one MARKET order and fill it (optionally in several
        partial fills whose quantities sum to ``base``)."""

        self._sequence += 1
        order_id = order_id or f"order-{self._sequence}"
        contract = replace(self.contract, product_id=product_id)
        if side is PaperSide.BUY:
            reserved = _floor(base * _ceil(evidence_price * Decimal("1.001"), CENT), CENT) * (
                Decimal("1.006")
            ) + Decimal(1)
            reserved = _ceil(reserved, CENT)
            self.account_state = replace(
                self.account_state, reserved_cash=self.account_state.reserved_cash + reserved
            )
            asset = "USD"
        else:
            reserved = base
            balance = self.account_state.balance("BTC")
            balances = (
                *(item for item in self.account_state.balances if item.asset != "BTC"),
                replace(balance, reserved=balance.reserved + base),
            )
            self.account_state = replace(self.account_state, balances=balances)
            asset = "BTC"
        order = CryptoPaperOrder(
            order_id=order_id,
            account_id=ACCOUNT_ID,
            intent_id=f"{order_id}-intent",
            preview_id=f"{order_id}-preview",
            authorization_id=f"{order_id}-authorization",
            product_id=product_id,
            base_asset="BTC",
            quote_asset="USD",
            side=side,
            order_type=PaperOrderType.MARKET,
            target_base_quantity=base,
            limit_price=None,
            filled_base_quantity=Decimal(0),
            filled_quote_quantity=Decimal(0),
            cumulative_fees=Decimal(0),
            state=PaperOrderState.OPEN,
            submitted_at=at,
            valid_until=at + timedelta(hours=4),
            last_sequence=1,
            canonical_hash="0" * 64,
        )
        reservation = CryptoPaperReservation(
            reservation_id=f"{order_id}-reservation",
            account_id=ACCOUNT_ID,
            order_id=order_id,
            asset=asset,
            original_amount=reserved,
            remaining_amount=reserved,
            consumed_amount=Decimal(0),
            state=PaperReservationState.HELD,
            version=1,
            canonical_hash="0" * 64,
        )
        for index, quantity in enumerate(fills or (base,)):
            observed = evidence_at or at + timedelta(minutes=1 + index)
            evidence = RetainedPaperPriceEvidence.create(
                evidence_id=evidence_id or f"{order_id}-evidence-{index}",
                product_id=product_id,
                observed_at=observed,
                ingested_at=observed,
                price=evidence_price,
                available_base_quantity=quantity,
                source=PaperPricingSource.GENUINE_TOP_OF_BOOK,
                evidence_class="HISTORICAL_PUBLIC",
            )
            applied = _apply(
                fill_id=fill_id or f"{order_id}-fill-{index}",
                order=order,
                contract=contract,
                reservation=reservation,
                account_state=self.account_state,
                position=self.positions.get(product_id),
                evidence=evidence,
                prior_fills=tuple(self.fills),
            )
            assert applied is not None
            order = applied.order
            reservation = applied.reservation
            self.account_state = applied.account_state
            self.positions[product_id] = applied.position
            self.fills.append(applied.fill)
            self.journals.append(applied.journal)
        self.orders.append(order)
        self.reservations.append(reservation)

    def reconcile(self, journals: tuple[CryptoPaperJournal, ...] | None = None):
        return reconcile_paper_state(
            reconciliation_id="reconciliation",
            account=ACCOUNT,
            account_state=self.account_state,
            orders=tuple(self.orders),
            reservations=tuple(self.reservations),
            fills=tuple(self.fills),
            positions=tuple(self.positions.values()),
            journals=tuple(self.journals) if journals is None else journals,
            evaluated_at=SELL_AT + timedelta(hours=1),
        )


def _apply(**kwargs: Any) -> CryptoPaperFillApplication | None:
    return apply_retained_evidence(
        fee_bps=FEE_BPS, slippage_bps=SLIPPAGE_BPS, actor="fixture-simulator", **kwargs
    )


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def _ceil(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def balances(journals: list[CryptoPaperJournal] | tuple[CryptoPaperJournal, ...]):
    """Signed ledger balances, debit-positive."""

    result: dict[str, Decimal] = {}
    for journal in journals:
        for posting in journal.postings:
            signed = posting.amount if posting.direction == "DEBIT" else -posting.amount
            result[posting.account_code] = result.get(posting.account_code, Decimal(0)) + signed
    return result


BUY_ORDER_ID = "strategy_paper_trial_recurring_candidate_c8e3ebc6004804b4dec2d4c0-order"
SELL_ORDER_ID = "strategy_paper_trial_generation_5f0024234415d261eb976b31-close-order"
WORKER = "apex-operating-cycle-plan-crypto-book-evidence-v1-worker"
BUY_FILL_ID = f"{BUY_ORDER_ID}-fill-{WORKER}-20260830"
SELL_FILL_ID = f"{SELL_ORDER_ID}-fill-{WORKER}-20260930"
BUY_EVIDENCE_ID = "live-observation-live_market_observation_3b87c5c1af5af59841197973-price-evidence"
SELL_EVIDENCE_ID = (
    "live-observation-live_market_observation_d8ce7b00f38f73acb6d645e7-price-evidence"
)
BUY_FILLED_AT = datetime(2026, 8, 30, 0, 4, 30, 814717, tzinfo=UTC)
SELL_FILLED_AT = datetime(2026, 9, 30, 0, 4, 9, 72783, tzinfo=UTC)
# Retained production canonical hashes (apex_crypto_paper_state_snapshot,
# genuine-strategy-crypto-paper-account, read 2026-09-30).
PRODUCTION_BUY_FILL_HASH = "15425047f7d2183b1b23ebb7ad669918181b289631d42f4745eb22a483e26489"
PRODUCTION_SELL_FILL_HASH = "caf3e70297c88e1691ae7f24bd2660273a01192fd931820f6af940e3d5e399af"
PRODUCTION_BUY_JOURNAL_HASH = "32a09d3e003dc4a073d0f2a3654674bfced219d5ca4f83d885ed20acbb933338"
PRODUCTION_SELL_JOURNAL_HASH = "777be9c2fe923dbaf8b3f94e9fe673b295b365e0bd80c90d27a1b128a733bebf"


def production_generation_1() -> Book:
    book = Book()
    book.trade(
        PaperSide.BUY,
        BASE,
        BUY_EVIDENCE_PRICE,
        BUY_AT,
        order_id=BUY_ORDER_ID,
        fill_id=BUY_FILL_ID,
        evidence_id=BUY_EVIDENCE_ID,
        evidence_at=BUY_FILLED_AT,
    )
    book.trade(
        PaperSide.SELL,
        BASE,
        SELL_EVIDENCE_PRICE,
        SELL_AT,
        order_id=SELL_ORDER_ID,
        fill_id=SELL_FILL_ID,
        evidence_id=SELL_EVIDENCE_ID,
        evidence_at=SELL_FILLED_AT,
    )
    return book


def _ledger_net_income(ledger: dict[str, Decimal]) -> Decimal:
    return -ledger.get("PAPER_REALIZED_PNL", Decimal(0)) - ledger.get(
        "PAPER_MODELED_FEE_EXPENSE", Decimal(0)
    )


# ---------------------------------------------------------------------------
# 1-4: the exact production generation-1 BUY + SELL
# ---------------------------------------------------------------------------


def test_production_fills_are_reproduced_exactly() -> None:
    book = production_generation_1()
    buy, sell = book.fills
    assert (buy.price, buy.base_quantity, buy.quote_quantity, buy.fee, buy.slippage) == (
        Decimal("78293.22"),
        BASE,
        Decimal("9.93"),
        Decimal("0.05"),
        Decimal("78.22"),
    )
    assert (sell.price, sell.base_quantity, sell.quote_quantity, sell.fee, sell.slippage) == (
        Decimal("83554.78"),
        BASE,
        Decimal("10.60"),
        Decimal("0.06"),
        Decimal("83.64"),
    )
    position = book.position()
    assert position is not None
    assert position.state == "CLOSED"
    assert position.base_quantity == 0
    assert position.cumulative_cost == 0
    assert position.realized_paper_pnl == Decimal("0.56")
    assert book.account_state.cash == Decimal("1000.56")
    assert book.account_state.realized_paper_pnl == Decimal("0.56")
    assert book.account_state.modeled_fees == Decimal("0.11")


def test_every_journal_balances_and_the_buy_journal_is_unchanged() -> None:
    book = production_generation_1()
    for journal in book.journals:
        debits = sum(item.amount for item in journal.postings if item.direction == "DEBIT")
        credits = sum(item.amount for item in journal.postings if item.direction == "CREDIT")
        assert debits == credits
    buy_journal = book.journals[0]
    assert [(p.account_code, p.direction, p.amount) for p in buy_journal.postings] == [
        ("PAPER_CRYPTO_ASSET_COST", "DEBIT", Decimal("9.93")),
        ("PAPER_MODELED_FEE_EXPENSE", "DEBIT", Decimal("0.05")),
        ("PAPER_CASH", "CREDIT", Decimal("9.98")),
    ]
    # BUY semantics are unchanged: byte-identical to production's journal.
    assert buy_journal.canonical_hash == PRODUCTION_BUY_JOURNAL_HASH
    assert [item.canonical_hash for item in book.fills] == [
        PRODUCTION_BUY_FILL_HASH,
        PRODUCTION_SELL_FILL_HASH,
    ]


def test_ledger_after_buy_only_is_consistent() -> None:
    book = Book()
    book.trade(PaperSide.BUY, BASE, BUY_EVIDENCE_PRICE, BUY_AT)
    ledger = balances(book.journals)
    assert ledger["PAPER_CRYPTO_ASSET_COST"] == Decimal("9.93")
    assert ledger["PAPER_MODELED_FEE_EXPENSE"] == Decimal("0.05")
    assert ledger["PAPER_CASH"] == Decimal("-9.98")
    assert book.reconcile().status is PaperReconciliationStatus.CLEAN


def test_full_close_leaves_zero_asset_cost_and_ledger_equals_closure_economics() -> None:
    book = production_generation_1()
    ledger = balances(book.journals)
    assert ledger["PAPER_CASH"] == Decimal("0.56")
    assert ledger.get("PAPER_CRYPTO_ASSET_COST", Decimal(0)) == 0
    assert ledger["PAPER_MODELED_FEE_EXPENSE"] == Decimal("0.11")
    assert -ledger["PAPER_REALIZED_PNL"] == Decimal("0.67")
    assert _ledger_net_income(ledger) == Decimal("0.56")
    position = book.position()
    assert position is not None
    assert _ledger_net_income(ledger) == position.realized_paper_pnl
    assert _ledger_net_income(ledger) == book.account_state.realized_paper_pnl
    assert ACCOUNT.opening_cash + ledger["PAPER_CASH"] == book.account_state.cash
    sell_journal = book.journals[1]
    assert [(p.account_code, p.direction, p.amount) for p in sell_journal.postings] == [
        ("PAPER_CASH", "DEBIT", Decimal("10.54")),
        ("PAPER_MODELED_FEE_EXPENSE", "DEBIT", Decimal("0.06")),
        ("PAPER_CRYPTO_ASSET_COST", "CREDIT", Decimal("9.93")),
        ("PAPER_REALIZED_PNL", "CREDIT", Decimal("0.67")),
    ]


def test_reconciliation_is_clean_after_a_correct_close() -> None:
    reconciliation = production_generation_1().reconcile()
    assert reconciliation.status is PaperReconciliationStatus.CLEAN
    assert reconciliation.discrepancies == ()


# ---------------------------------------------------------------------------
# 9: reconciliation detects the pre-fix impossible state
# ---------------------------------------------------------------------------


def _production_defective_sell_journal(book: Book) -> CryptoPaperJournal:
    """The exact SELL journal production retained (credits the fee-inclusive
    position basis 9.98 and a 0.62 P&L)."""

    from apex.domain.crypto_paper import CryptoPaperPosting
    from apex.domain.values import content_digest

    sell = book.fills[1]
    postings = (
        CryptoPaperPosting("PAPER_CASH", "DEBIT", Decimal("10.54")),
        CryptoPaperPosting("PAPER_MODELED_FEE_EXPENSE", "DEBIT", Decimal("0.06")),
        CryptoPaperPosting("PAPER_CRYPTO_ASSET_COST", "CREDIT", Decimal("9.98")),
        CryptoPaperPosting("PAPER_REALIZED_PNL", "CREDIT", Decimal("0.62")),
    )
    values = (
        f"{sell.fill_id}-journal",
        sell.fill_id,
        ACCOUNT_ID,
        "USD",
        postings,
        sell.event_time,
        "PAPER_ONLY",
    )
    return CryptoPaperJournal(*values[:-1], content_digest(values), *values[-1:])


def test_defective_sell_journal_is_byte_identical_to_production() -> None:
    book = production_generation_1()
    assert _production_defective_sell_journal(book).canonical_hash == (PRODUCTION_SELL_JOURNAL_HASH)


def test_reconciliation_detects_the_production_impossible_state() -> None:
    book = production_generation_1()
    defective = (book.journals[0], _production_defective_sell_journal(book))
    ledger = balances(defective)
    assert ledger["PAPER_CRYPTO_ASSET_COST"] == Decimal("-0.05")
    assert _ledger_net_income(ledger) == Decimal("0.51")
    reconciliation = book.reconcile(defective)
    assert reconciliation.status is PaperReconciliationStatus.DISCREPANCY
    assert "LEDGER_ASSET_COST_MISMATCH:BTC-USD" in reconciliation.discrepancies
    assert "FLAT_POSITION_LEDGER_ASSET_COST_RESIDUAL:BTC-USD" in reconciliation.discrepancies
    assert "LEDGER_NET_INCOME_MISMATCH" in reconciliation.discrepancies


# ---------------------------------------------------------------------------
# 5-7: partial closes, repeated buys, and multiple fills
# ---------------------------------------------------------------------------


def _assert_ledger_contract(book: Book) -> None:
    ledger = balances(book.journals)
    consideration = Decimal(0)
    for position in book.positions.values():
        if position.base_quantity == 0:
            continue
        consideration += ledger_consideration(book, position.product_id)
    assert ledger.get("PAPER_CRYPTO_ASSET_COST", Decimal(0)) == consideration
    capitalized_fees = (
        sum((item.cumulative_cost for item in book.positions.values()), Decimal(0)) - consideration
    )
    assert _ledger_net_income(ledger) == book.account_state.realized_paper_pnl - capitalized_fees
    assert ACCOUNT.opening_cash + ledger.get("PAPER_CASH", Decimal(0)) == book.account_state.cash
    assert ledger.get("PAPER_MODELED_FEE_EXPENSE", Decimal(0)) == sum(
        (item.fee for item in book.fills), Decimal(0)
    )
    assert book.reconcile().status is PaperReconciliationStatus.CLEAN


def ledger_consideration(book: Book, product_id: str) -> Decimal:
    """Independent recomputation: fee-exclusive consideration remaining,
    relieved proportionally (rounded up to the satoshi) on partial SELLs and
    exactly on the SELL that flattens the position."""

    quantity = Decimal(0)
    consideration = Decimal(0)
    for fill in book.fills:
        if fill.product_id != product_id:
            continue
        if fill.side is PaperSide.BUY:
            quantity += fill.base_quantity
            consideration += fill.quote_quantity
        else:
            relief = (
                consideration
                if fill.base_quantity == quantity
                else _ceil(consideration * fill.base_quantity / quantity, SATOSHI)
            )
            quantity -= fill.base_quantity
            consideration -= relief
    return consideration


def test_partial_close_relieves_proportional_consideration() -> None:
    book = Book()
    book.trade(PaperSide.BUY, Decimal("0.0003"), Decimal("50000"), BUY_AT)
    buy = book.fills[0]
    book.trade(PaperSide.SELL, Decimal("0.0001"), Decimal("55000"), SELL_AT)
    sell_journal = book.journals[-1]
    relieved = next(
        item.amount
        for item in sell_journal.postings
        if item.account_code == "PAPER_CRYPTO_ASSET_COST"
    )
    assert relieved == _ceil(buy.quote_quantity / 3, SATOSHI)
    _assert_ledger_contract(book)


def test_final_partial_close_reaches_exact_zero() -> None:
    book = Book()
    book.trade(PaperSide.BUY, Decimal("0.0003"), Decimal("50000"), BUY_AT)
    for day in range(3):
        book.trade(
            PaperSide.SELL, Decimal("0.0001"), Decimal("55000"), SELL_AT + timedelta(days=day)
        )
        _assert_ledger_contract(book)
    position = book.position()
    assert position is not None and position.base_quantity == 0
    ledger = balances(book.journals)
    assert ledger.get("PAPER_CRYPTO_ASSET_COST", Decimal(0)) == 0
    assert _ledger_net_income(ledger) == book.account_state.realized_paper_pnl


def test_repeated_buys_partial_fills_and_reopen_preserve_the_invariant() -> None:
    book = Book()
    book.trade(
        PaperSide.BUY,
        Decimal("0.00030000"),
        Decimal("61234.57"),
        BUY_AT,
        fills=(Decimal("0.00010001"), Decimal("0.00019999")),
    )
    book.trade(PaperSide.BUY, Decimal("0.00012687"), BUY_EVIDENCE_PRICE, BUY_AT + timedelta(days=1))
    _assert_ledger_contract(book)
    book.trade(
        PaperSide.SELL,
        Decimal("0.00020000"),
        Decimal("70000.01"),
        SELL_AT,
        fills=(Decimal("0.00007777"), Decimal("0.00012223")),
    )
    _assert_ledger_contract(book)
    position = book.position()
    assert position is not None
    book.trade(PaperSide.SELL, position.base_quantity, Decimal("59999.99"), SELL_AT + timedelta(1))
    _assert_ledger_contract(book)
    assert balances(book.journals).get("PAPER_CRYPTO_ASSET_COST", Decimal(0)) == 0
    # A later generation reopens the same position identity.
    book.trade(PaperSide.BUY, BASE, BUY_EVIDENCE_PRICE, SELL_AT + timedelta(days=2))
    book.trade(PaperSide.SELL, BASE, SELL_EVIDENCE_PRICE, SELL_AT + timedelta(days=33))
    _assert_ledger_contract(book)
    assert balances(book.journals).get("PAPER_CRYPTO_ASSET_COST", Decimal(0)) == 0


@pytest.mark.parametrize("seed", range(12))
def test_randomized_fill_sequences_never_drift(seed: int) -> None:
    rng = random.Random(seed)
    book = Book()
    at = BUY_AT
    for _ in range(10):
        at += timedelta(days=1)
        position = book.position()
        held = Decimal(0) if position is None else position.base_quantity
        price = Decimal(rng.randint(2_000_000, 12_000_000)) / 100
        if held == 0 or rng.random() < 0.45:
            base = Decimal(rng.randint(2_000, 40_000)) * SATOSHI
            book.trade(PaperSide.BUY, base, price, at)
        else:
            base = held if rng.random() < 0.3 else max(SATOSHI, _floor(held / 3, SATOSHI))
            parts = (base,)
            if base > 2 * SATOSHI and rng.random() < 0.5:
                first = _floor(base / 2, SATOSHI)
                parts = (first, base - first)
            book.trade(PaperSide.SELL, base, price, at, fills=parts)
        _assert_ledger_contract(book)
    position = book.position()
    if position is not None and position.base_quantity:
        book.trade(PaperSide.SELL, position.base_quantity, Decimal("50000"), at + timedelta(1))
    _assert_ledger_contract(book)
    assert balances(book.journals).get("PAPER_CRYPTO_ASSET_COST", Decimal(0)) == 0


def test_flattening_sell_relieves_exact_position_cost_with_no_rounding_residue() -> None:
    book = Book()
    book.trade(PaperSide.BUY, Decimal("0.0003"), Decimal("50000"), BUY_AT)
    for day in range(3):
        book.trade(
            PaperSide.SELL, Decimal("0.0001"), Decimal("55000"), SELL_AT + timedelta(days=day)
        )
    position = book.position()
    assert position is not None
    assert position.state == "CLOSED"
    assert position.cumulative_cost == 0
    cash_result = book.account_state.cash - ACCOUNT.opening_cash
    assert position.realized_paper_pnl == cash_result
    assert book.account_state.realized_paper_pnl == cash_result


def test_sell_refuses_a_fill_history_inconsistent_with_the_position() -> None:
    book = Book()
    book.trade(PaperSide.BUY, BASE, BUY_EVIDENCE_PRICE, BUY_AT)
    with pytest.raises(CryptoPaperError, match="POSITION_FILL_HISTORY_MISMATCH"):
        book.fills.clear()
        book.trade(PaperSide.SELL, BASE, SELL_EVIDENCE_PRICE, SELL_AT)


# ---------------------------------------------------------------------------
# 11-16: append-only production correction
# ---------------------------------------------------------------------------


def _defective_state() -> tuple[Book, CryptoPaperState]:
    book = production_generation_1()
    journals = (book.journals[0], _production_defective_sell_journal(book))
    state = CryptoPaperState(
        account=ACCOUNT,
        account_state=book.account_state,
        orders=tuple(book.orders),
        reservations=tuple(book.reservations),
        fills=tuple(book.fills),
        positions=tuple(book.positions.values()),
        journals=journals,
    ).sealed()
    return book, state


def seed_defective_production_state(repository: Any) -> CryptoPaperState:
    """Seeds an account whose latest snapshot is production's post-close
    state (defective SELL journal included) through the repository's own
    append-only write path."""

    opening = CryptoPaperState(
        account=ACCOUNT,
        account_state=CryptoPaperAccountState.opening(account=ACCOUNT, event_id="created"),
    ).sealed()
    repository.save_account(account=ACCOUNT, state=opening, idempotency_key="seed-account")
    _, defective = _defective_state()
    operation = CryptoPaperOperation.create(
        idempotency_key="seed-production-close",
        operation_type="APPLY_EVIDENCE",
        result_id=SELL_FILL_ID,
        result_hash=PRODUCTION_SELL_FILL_HASH,
    )
    seeded = replace(defective, operations=(operation,)).sealed()
    repository.save_state(
        state=seeded, prior_state_hash=opening.canonical_hash, operation=operation
    )
    return seeded


def _correct(repository: Any, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "sell_fill_id": SELL_FILL_ID,
        "expected_source_journal_hash": PRODUCTION_SELL_JOURNAL_HASH,
        "expected_restatement": "0.05",
        "idempotency_key": "crypto-paper-close-accounting-correction-v1:generation-1",
    }
    arguments.update(overrides)
    return CorrectCryptoPaperCloseAccounting(repository).execute(**arguments)


def test_correction_is_deterministic_balanced_and_restores_the_contract() -> None:
    _, state = _defective_state()
    first = build_close_accounting_correction(
        account_id=ACCOUNT_ID, fills=state.fills, journals=state.journals, sell_fill_id=SELL_FILL_ID
    )
    second = build_close_accounting_correction(
        account_id=ACCOUNT_ID, fills=state.fills, journals=state.journals, sell_fill_id=SELL_FILL_ID
    )
    assert first == second
    assert first.booked_asset_cost_relief == Decimal("9.98")
    assert first.contract_asset_cost_relief == Decimal("9.93")
    assert first.restatement == Decimal("0.05")
    assert first.source_journal_hash == PRODUCTION_SELL_JOURNAL_HASH
    journal = first.journal
    assert journal.journal_id == f"{SELL_FILL_ID}-journal-close-accounting-correction-v1"
    assert journal.source_fill_id == SELL_FILL_ID
    assert journal.occurred_at == SELL_FILLED_AT
    assert journal.authority == "PAPER_ONLY"
    assert [(p.account_code, p.direction, p.amount) for p in journal.postings] == [
        ("PAPER_CRYPTO_ASSET_COST", "DEBIT", Decimal("0.05")),
        ("PAPER_REALIZED_PNL", "CREDIT", Decimal("0.05")),
    ]
    corrected = (*state.journals, journal)
    ledger = balances(corrected)
    assert ledger["PAPER_CRYPTO_ASSET_COST"] == 0
    assert -ledger["PAPER_REALIZED_PNL"] == Decimal("0.67")
    assert ledger["PAPER_MODELED_FEE_EXPENSE"] == Decimal("0.11")
    assert _ledger_net_income(ledger) == Decimal("0.56")
    assert ledger["PAPER_CASH"] == Decimal("0.56")
    assert (
        ledger_contract_discrepancies(
            account_state=state.account_state,
            fills=state.fills,
            positions=state.positions,
            journals=corrected,
        )
        == ()
    )


def test_correction_service_inserts_once_replays_as_no_op_and_changes_nothing_else() -> None:
    repository = InMemoryCryptoPaperRepository()
    seeded = seed_defective_production_state(repository)
    result = _correct(repository)
    assert result.idempotent_replay is False
    assert result.live_authority == "NONE"
    after = repository.state(ACCOUNT_ID)
    assert after is not None
    assert after.journals[:2] == seeded.journals
    assert [item.canonical_hash for item in after.journals[:2]] == [
        PRODUCTION_BUY_JOURNAL_HASH,
        PRODUCTION_SELL_JOURNAL_HASH,
    ]
    assert after.journals[2] == result.value
    for name in ("account", "account_state", "orders", "reservations", "fills", "positions"):
        assert getattr(after, name) == getattr(seeded, name)
    assert after.account_state.cash == Decimal("1000.56")
    assert after.positions[0].realized_paper_pnl == Decimal("0.56")
    assert after.operations[-1].operation_type == "CORRECT_CLOSE_ACCOUNTING"
    replay = _correct(repository)
    assert replay.idempotent_replay is True
    assert replay.value == result.value
    assert repository.state(ACCOUNT_ID) == after
    with pytest.raises(CryptoPaperError, match="CORRECTION_ALREADY_RETAINED"):
        _correct(repository, idempotency_key="a-different-key")
    reconciliation = ReconcileCryptoPaperAccount(repository).execute(
        account_id=ACCOUNT_ID,
        reconciliation_id="post-correction",
        evaluated_at=SELL_AT + timedelta(hours=1),
        order_id=None,
        idempotency_key="post-correction",
    )
    assert reconciliation.value.status is PaperReconciliationStatus.CLEAN


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expected_source_journal_hash": "0" * 64}, "CORRECTION_SOURCE_HASH_MISMATCH"),
        ({"expected_restatement": "0.04"}, "CORRECTION_AMOUNT_MISMATCH"),
        ({"expected_restatement": "-0.05"}, "CORRECTION_AMOUNT_MISMATCH"),
        ({"sell_fill_id": BUY_FILL_ID}, "CORRECTION_SOURCE_NOT_SELL"),
        ({"sell_fill_id": "unknown-fill"}, "CORRECTION_SOURCE_FILL_NOT_FOUND"),
    ],
)
def test_correction_refuses_unexpected_requests(overrides: dict[str, Any], reason: str) -> None:
    repository = InMemoryCryptoPaperRepository()
    seeded = seed_defective_production_state(repository)
    with pytest.raises(CryptoPaperError, match=reason):
        _correct(repository, **overrides)
    assert repository.state(ACCOUNT_ID) == seeded


def test_correction_refuses_a_correct_post_fix_close() -> None:
    book = production_generation_1()
    with pytest.raises(CryptoPaperError, match="CORRECTION_NOT_REQUIRED"):
        build_close_accounting_correction(
            account_id=ACCOUNT_ID,
            fills=tuple(book.fills),
            journals=tuple(book.journals),
            sell_fill_id=SELL_FILL_ID,
        )


def test_correction_refuses_a_source_journal_not_produced_by_the_pre_fix_formula() -> None:
    _, state = _defective_state()
    tampered_source = replace(
        state.journals[1], occurred_at=state.journals[1].occurred_at + timedelta(seconds=1)
    )
    with pytest.raises(CryptoPaperError, match="NOT_PRE_FIX_SELL"):
        build_close_accounting_correction(
            account_id=ACCOUNT_ID,
            fills=state.fills,
            journals=(state.journals[0], tampered_source),
            sell_fill_id=SELL_FILL_ID,
        )


def test_correction_refuses_when_other_ledger_discrepancies_remain() -> None:
    repository = InMemoryCryptoPaperRepository()
    seed_defective_production_state(repository)
    state = repository.state(ACCOUNT_ID)
    assert state is not None
    drifted = replace(
        state,
        account_state=replace(state.account_state, modeled_fees=Decimal("0.12")),
    ).sealed()
    operation = CryptoPaperOperation.create(
        idempotency_key="drift", operation_type="TEST", result_id="drift", result_hash="d" * 64
    )
    repository.save_state(
        state=replace(drifted, operations=(*drifted.operations, operation)).sealed(),
        prior_state_hash=state.canonical_hash,
        operation=operation,
    )
    with pytest.raises(CryptoPaperError, match="DOES_NOT_RESTORE_LEDGER_CONTRACT"):
        _correct(repository)
