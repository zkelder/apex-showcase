"""Crypto PAPER Ledger Close Accounting Integrity v1 -- real PostgreSQL.

Seeds production's exact post-close generation-1 state (byte-identical fill
and journal hashes) through the append-only state-snapshot write path, then
proves the guarded correction inserts exactly one new snapshot, is a no-op
on replay, refuses unexpected source state, and leaves every retained row
and every original journal byte-identical.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository, state_table
from apex.application.crypto_paper import ReconcileCryptoPaperAccount
from apex.domain.crypto_paper import (
    CryptoPaperError,
    PaperReconciliationStatus,
    ledger_account_balances,
    ledger_contract_discrepancies,
)
from tests.test_crypto_paper_ledger_close_accounting import (
    ACCOUNT_ID,
    PRODUCTION_BUY_JOURNAL_HASH,
    PRODUCTION_SELL_JOURNAL_HASH,
    SELL_AT,
    _correct,
    seed_defective_production_state,
)

pytestmark = pytest.mark.postgres


def _snapshots(engine: sa.Engine) -> list[dict[str, object]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(state_table)
            .where(state_table.c.account_id == ACCOUNT_ID)
            .order_by(state_table.c.sequence)
        ).mappings()
        return [dict(row) for row in rows]


def test_seeded_production_state_round_trips_and_shows_the_defect(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    seeded = seed_defective_production_state(repository)
    retained = repository.state(ACCOUNT_ID)
    assert retained == seeded
    assert [item.canonical_hash for item in retained.journals] == [
        PRODUCTION_BUY_JOURNAL_HASH,
        PRODUCTION_SELL_JOURNAL_HASH,
    ]
    ledger = ledger_account_balances(retained.journals)
    assert ledger["PAPER_CRYPTO_ASSET_COST"] == Decimal("-0.05")
    assert -ledger["PAPER_REALIZED_PNL"] - ledger["PAPER_MODELED_FEE_EXPENSE"] == Decimal("0.51")
    assert set(
        ledger_contract_discrepancies(
            account_state=retained.account_state,
            fills=retained.fills,
            positions=retained.positions,
            journals=retained.journals,
        )
    ) == {
        "LEDGER_ASSET_COST_MISMATCH:BTC-USD",
        "FLAT_POSITION_LEDGER_ASSET_COST_RESIDUAL:BTC-USD",
        "LEDGER_NET_INCOME_MISMATCH",
    }


def test_correction_inserts_one_snapshot_once_and_mutates_nothing(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    seeded = seed_defective_production_state(repository)
    before_rows = _snapshots(postgres_engine)

    result = _correct(repository)
    assert result.idempotent_replay is False
    after_rows = _snapshots(postgres_engine)
    assert after_rows[: len(before_rows)] == before_rows  # retained rows byte-identical
    assert len(after_rows) == len(before_rows) + 1
    new_row = after_rows[-1]
    assert new_row["operation_type"] == "CORRECT_CLOSE_ACCOUNTING"
    assert new_row["prior_state_hash"] == before_rows[-1]["canonical_hash"]
    assert new_row["authority"] == "PAPER_ONLY"
    assert new_row["live_authority"] == "NONE"
    assert new_row["provider_requests"] == 0

    corrected = repository.state(ACCOUNT_ID)
    assert corrected is not None
    # Original journals byte-identical (payload and hash), correction appended.
    old_payload = json.loads(str(before_rows[-1]["payload"]))
    new_payload = json.loads(str(new_row["payload"]))
    assert new_payload["journals"][:2] == old_payload["journals"]
    for name in ("account", "account_state", "orders", "reservations", "fills", "positions"):
        assert new_payload[name] == old_payload[name]
        assert getattr(corrected, name) == getattr(seeded, name)
    assert corrected.journals[2] == result.value
    ledger = ledger_account_balances(corrected.journals)
    assert ledger["PAPER_CRYPTO_ASSET_COST"] == 0
    assert -ledger["PAPER_REALIZED_PNL"] == Decimal("0.67")
    assert ledger["PAPER_MODELED_FEE_EXPENSE"] == Decimal("0.11")
    assert ledger["PAPER_CASH"] == Decimal("0.56")
    assert corrected.account_state.cash == Decimal("1000.56")
    assert corrected.positions[0].realized_paper_pnl == Decimal("0.56")

    replay = _correct(repository)
    assert replay.idempotent_replay is True
    assert replay.value == result.value
    assert _snapshots(postgres_engine) == after_rows
    with pytest.raises(CryptoPaperError, match="CORRECTION_ALREADY_RETAINED"):
        _correct(repository, idempotency_key="another-key")
    assert _snapshots(postgres_engine) == after_rows

    reconciliation = ReconcileCryptoPaperAccount(repository).execute(
        account_id=ACCOUNT_ID,
        reconciliation_id="post-correction-reconciliation",
        evaluated_at=SELL_AT + timedelta(hours=2),
        order_id=None,
        idempotency_key="post-correction-reconciliation",
    )
    assert reconciliation.value.status is PaperReconciliationStatus.CLEAN


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_source_journal_hash": "f" * 64},
        {"expected_restatement": "0.06"},
    ],
)
def test_correction_refuses_unexpected_state_without_writing(
    postgres_engine: sa.Engine, overrides: dict[str, str]
) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    seed_defective_production_state(repository)
    before_rows = _snapshots(postgres_engine)
    with pytest.raises(CryptoPaperError, match="MISMATCH"):
        _correct(repository, **overrides)
    assert _snapshots(postgres_engine) == before_rows
