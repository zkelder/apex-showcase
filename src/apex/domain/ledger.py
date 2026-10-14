"""Authoritative in-memory double-entry ledger for the golden slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import Money, ValueValidationError, deterministic_id, parse_decimal


class PostingSide(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class AccountCode(StrEnum):
    CASH = "CASH"
    POSITION_COST = "POSITION_COST"
    CONTRIBUTED_CAPITAL = "CONTRIBUTED_CAPITAL"
    FEE_EXPENSE = "FEE_EXPENSE"
    REALIZED_PNL = "REALIZED_PNL"
    DIVIDEND_INCOME = "DIVIDEND_INCOME"


class JournalType(StrEnum):
    CONTRIBUTION = "CONTRIBUTION"
    PURCHASE = "PURCHASE"
    FEE = "FEE"
    SETTLEMENT = "SETTLEMENT"
    SALE = "SALE"
    DIVIDEND = "DIVIDEND"


class LedgerConflictError(ValueError):
    """A source identity was reused with a different journal payload."""


@dataclass(frozen=True, slots=True)
class Posting:
    account_code: AccountCode
    side: PostingSide
    amount: Money

    def __post_init__(self) -> None:
        if self.amount.amount <= 0:
            raise ValueValidationError("ledger posting amounts must be positive")


@dataclass(frozen=True, slots=True)
class Journal:
    journal_id: str
    source_kind: str
    source_id: str
    journal_type: JournalType
    occurred_at: datetime
    postings: tuple[Posting, ...]

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueValidationError("journal occurrence time must be timezone-aware")
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(UTC))
        if len(self.postings) < 2:
            raise ValueValidationError("a journal requires at least two postings")
        currencies = {posting.amount.currency for posting in self.postings}
        if currencies != {"USD"}:
            raise ValueValidationError("golden journals must contain USD postings only")
        debits = sum(
            (
                posting.amount.amount
                for posting in self.postings
                if posting.side is PostingSide.DEBIT
            ),
            Decimal(0),
        )
        credits = sum(
            (
                posting.amount.amount
                for posting in self.postings
                if posting.side is PostingSide.CREDIT
            ),
            Decimal(0),
        )
        if debits != credits:
            raise ValueValidationError("journal debits and credits must balance exactly")

    @property
    def balanced(self) -> bool:
        debits = sum(
            (
                posting.amount.amount
                for posting in self.postings
                if posting.side is PostingSide.DEBIT
            ),
            Decimal(0),
        )
        credits = sum(
            (
                posting.amount.amount
                for posting in self.postings
                if posting.side is PostingSide.CREDIT
            ),
            Decimal(0),
        )
        return debits == credits


class JournalFactory:
    @staticmethod
    def contribution(source_id: str, amount: Money, occurred_at: datetime) -> Journal:
        return JournalFactory._build(
            source_kind="campaign_contribution",
            source_id=source_id,
            journal_type=JournalType.CONTRIBUTION,
            occurred_at=occurred_at,
            postings=(
                Posting(AccountCode.CASH, PostingSide.DEBIT, amount),
                Posting(AccountCode.CONTRIBUTED_CAPITAL, PostingSide.CREDIT, amount),
            ),
        )

    @staticmethod
    def purchase(source_id: str, amount: Money, occurred_at: datetime) -> Journal:
        return JournalFactory._build(
            source_kind="execution_purchase",
            source_id=source_id,
            journal_type=JournalType.PURCHASE,
            occurred_at=occurred_at,
            postings=(
                Posting(AccountCode.POSITION_COST, PostingSide.DEBIT, amount),
                Posting(AccountCode.CASH, PostingSide.CREDIT, amount),
            ),
        )

    @staticmethod
    def fee(source_id: str, amount: Money, occurred_at: datetime) -> Journal:
        return JournalFactory._build(
            source_kind="execution_fee",
            source_id=source_id,
            journal_type=JournalType.FEE,
            occurred_at=occurred_at,
            postings=(
                Posting(AccountCode.FEE_EXPENSE, PostingSide.DEBIT, amount),
                Posting(AccountCode.CASH, PostingSide.CREDIT, amount),
            ),
        )

    @staticmethod
    def settlement(
        source_id: str,
        proceeds: Money,
        cost_basis: Money,
        occurred_at: datetime,
    ) -> Journal:
        postings: list[Posting] = []
        if proceeds.amount > 0:
            postings.append(Posting(AccountCode.CASH, PostingSide.DEBIT, proceeds))
        if proceeds.amount >= cost_basis.amount:
            gain = proceeds.amount - cost_basis.amount
            postings.append(Posting(AccountCode.POSITION_COST, PostingSide.CREDIT, cost_basis))
            if gain > 0:
                postings.append(Posting(AccountCode.REALIZED_PNL, PostingSide.CREDIT, Money(gain)))
        else:
            loss = cost_basis.amount - proceeds.amount
            postings.append(Posting(AccountCode.REALIZED_PNL, PostingSide.DEBIT, Money(loss)))
            postings.append(Posting(AccountCode.POSITION_COST, PostingSide.CREDIT, cost_basis))
        return JournalFactory._build(
            source_kind="settlement",
            source_id=source_id,
            journal_type=JournalType.SETTLEMENT,
            occurred_at=occurred_at,
            postings=tuple(postings),
        )

    @staticmethod
    def sale(
        source_id: str,
        proceeds: Money,
        cost_basis: Money,
        occurred_at: datetime,
    ) -> Journal:
        """Derecognize continuous-market cost and retain exact sale P&L."""

        postings: list[Posting] = [
            Posting(AccountCode.CASH, PostingSide.DEBIT, proceeds),
            Posting(AccountCode.POSITION_COST, PostingSide.CREDIT, cost_basis),
        ]
        if proceeds.amount > cost_basis.amount:
            postings.append(
                Posting(
                    AccountCode.REALIZED_PNL,
                    PostingSide.CREDIT,
                    Money(proceeds.amount - cost_basis.amount),
                )
            )
        elif proceeds.amount < cost_basis.amount:
            postings.append(
                Posting(
                    AccountCode.REALIZED_PNL,
                    PostingSide.DEBIT,
                    Money(cost_basis.amount - proceeds.amount),
                )
            )
        return JournalFactory._build(
            source_kind="securities_sale",
            source_id=source_id,
            journal_type=JournalType.SALE,
            occurred_at=occurred_at,
            postings=tuple(postings),
        )

    @staticmethod
    def dividend(source_id: str, amount: Money, occurred_at: datetime) -> Journal:
        """Recognize one retained cash-dividend entitlement."""

        return JournalFactory._build(
            source_kind="securities_dividend",
            source_id=source_id,
            journal_type=JournalType.DIVIDEND,
            occurred_at=occurred_at,
            postings=(
                Posting(AccountCode.CASH, PostingSide.DEBIT, amount),
                Posting(AccountCode.DIVIDEND_INCOME, PostingSide.CREDIT, amount),
            ),
        )

    @staticmethod
    def _build(
        *,
        source_kind: str,
        source_id: str,
        journal_type: JournalType,
        occurred_at: datetime,
        postings: tuple[Posting, ...],
    ) -> Journal:
        payload = {
            "source_kind": source_kind,
            "source_id": source_id,
            "journal_type": journal_type,
            "occurred_at": occurred_at,
            "postings": postings,
        }
        return Journal(
            journal_id=deterministic_id("journal", payload),
            source_kind=source_kind,
            source_id=source_id,
            journal_type=journal_type,
            occurred_at=occurred_at,
            postings=postings,
        )


@dataclass(frozen=True, slots=True)
class LedgerMetrics:
    contributions: Money
    cash: Money
    position_cost: Money
    ledger_equity: Money
    realized_trading_pnl_before_fees: Decimal
    dividend_income: Money
    fees: Money
    net_trading_pnl: Decimal


class Ledger:
    """Append-only-by-contract journal set with source-level idempotency."""

    def __init__(self) -> None:
        self._journals: dict[tuple[str, str], Journal] = {}

    @property
    def journals(self) -> tuple[Journal, ...]:
        return tuple(sorted(self._journals.values(), key=lambda journal: journal.journal_id))

    def post(self, journal: Journal) -> Journal:
        return self.post_many((journal,))[0]

    def post_many(self, journals: tuple[Journal, ...]) -> tuple[Journal, ...]:
        """Validate a journal group completely before applying any new entry."""

        staged: dict[tuple[str, str], Journal] = {}
        results: list[Journal] = []
        for journal in journals:
            if not journal.balanced:
                raise ValueValidationError("unbalanced journal cannot be posted")
            identity = (journal.source_kind, journal.source_id)
            existing = staged.get(identity, self._journals.get(identity))
            if existing is not None:
                if existing != journal:
                    raise LedgerConflictError("journal source identity has a conflicting payload")
                results.append(existing)
                continue
            staged[identity] = journal
            results.append(journal)
        self._journals.update(staged)
        return tuple(results)

    def account_balance(self, account_code: AccountCode) -> Decimal:
        debit = Decimal(0)
        credit = Decimal(0)
        for journal in self._journals.values():
            for posting in journal.postings:
                if posting.account_code is not account_code:
                    continue
                if posting.side is PostingSide.DEBIT:
                    debit += posting.amount.amount
                else:
                    credit += posting.amount.amount
        return parse_decimal(debit - credit, allow_negative=True)

    def metrics(self) -> LedgerMetrics:
        cash = self.account_balance(AccountCode.CASH)
        position_cost = self.account_balance(AccountCode.POSITION_COST)
        contributions = -self.account_balance(AccountCode.CONTRIBUTED_CAPITAL)
        fees = self.account_balance(AccountCode.FEE_EXPENSE)
        realized = -self.account_balance(AccountCode.REALIZED_PNL)
        dividend_income = -self.account_balance(AccountCode.DIVIDEND_INCOME)
        equity = cash + position_cost
        net_pnl = realized + dividend_income - fees
        return LedgerMetrics(
            contributions=Money(contributions),
            cash=Money(cash),
            position_cost=Money(position_cost),
            ledger_equity=Money(equity),
            realized_trading_pnl_before_fees=parse_decimal(realized, allow_negative=True),
            dividend_income=Money(dividend_income),
            fees=Money(fees),
            net_trading_pnl=parse_decimal(net_pnl, allow_negative=True),
        )
