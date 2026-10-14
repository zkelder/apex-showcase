from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.ledger import (
    JournalFactory,
    Ledger,
    LedgerConflictError,
)
from apex.domain.values import Money

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)


def _golden_journals() -> tuple[object, ...]:
    return (
        JournalFactory.contribution("funding-1", Money("1000"), NOW),
        JournalFactory.purchase("fill-1", Money("40"), NOW),
        JournalFactory.fee("fill-1", Money("1"), NOW),
        JournalFactory.settlement("settlement-1", Money("100"), Money("40"), NOW),
    )


def test_each_required_journal_balances() -> None:
    journals = _golden_journals()
    assert len(journals) == 4
    assert all(journal.balanced for journal in journals)  # type: ignore[attr-defined]
    assert all(len(journal.postings) >= 2 for journal in journals)  # type: ignore[attr-defined]


def test_ledger_reconciles_equity_and_excludes_funding_from_trading_profit() -> None:
    ledger = Ledger()
    for journal in _golden_journals():
        ledger.post(journal)  # type: ignore[arg-type]
    metrics = ledger.metrics()
    assert metrics.contributions.amount == Decimal("1000")
    assert metrics.realized_trading_pnl_before_fees == Decimal("60")
    assert metrics.fees.amount == Decimal("1")
    assert metrics.net_trading_pnl == Decimal("59")
    assert metrics.cash.amount == Decimal("1059")
    assert metrics.position_cost.amount == Decimal("0")
    assert metrics.ledger_equity.amount == Decimal("1059")
    assert metrics.ledger_equity.amount == metrics.contributions.amount + metrics.net_trading_pnl


def test_funding_only_has_zero_trading_pnl() -> None:
    ledger = Ledger()
    ledger.post(JournalFactory.contribution("funding-only", Money("250"), NOW))
    metrics = ledger.metrics()
    assert metrics.ledger_equity.amount == Decimal("250")
    assert metrics.net_trading_pnl == Decimal("0")


def test_duplicate_journal_source_is_idempotent_and_conflict_fails_closed() -> None:
    ledger = Ledger()
    journal = JournalFactory.contribution("funding-1", Money("1000"), NOW)
    assert ledger.post(journal) is journal
    assert ledger.post(journal) is journal
    before = ledger.journals
    conflicting = replace(journal, journal_id="journal_conflicting")
    with pytest.raises(LedgerConflictError):
        ledger.post(conflicting)
    assert ledger.journals == before


def test_grouped_fill_journals_validate_atomically_before_posting() -> None:
    ledger = Ledger()
    existing_fee = JournalFactory.fee("fill-conflict", Money("1"), NOW)
    ledger.post(existing_fee)
    before = ledger.journals
    purchase = JournalFactory.purchase("new-fill", Money("39"), NOW)
    conflicting_fee = JournalFactory.fee("fill-conflict", Money("2"), NOW)

    with pytest.raises(LedgerConflictError):
        ledger.post_many((purchase, conflicting_fee))

    assert ledger.journals == before
