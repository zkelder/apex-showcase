from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.crypto_paper_acceptance import (
    ACCEPTANCE_ACCOUNT_ID,
    run_crypto_paper_fixture_acceptance,
)
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.application.crypto_paper import (
    CreateCryptoPaperAccount,
    RegisterCryptoPaperProviderContract,
    RetainCryptoPaperCapability,
)
from apex.domain.crypto_paper import (
    CryptoPaperOperation,
    ProviderCapabilityState,
    build_provider_contract,
    inspect_paper_capability,
)
from apex.domain.crypto_universe import PublicCryptoProduct
from apex.domain.research_evidence import EvidenceClass

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


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
        captured_at=NOW,
        source_contract_version="COINBASE_PUBLIC_PRODUCTS_CURSOR_V1",
        raw_evidence_hash="a" * 64,
        parser_version="CRYPTO_PRODUCT_PARSER_V1",
        evidence_class=EvidenceClass.FIXTURE,
    )


def test_crypto_paper_contract_capability_and_state_are_append_only_and_replayable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    contract = build_provider_contract(
        contract_id="postgres-crypto-paper-btc-usd-contract",
        product=_product(),
        source_capture_id="postgres-fixture-capture",
    )
    registered = RegisterCryptoPaperProviderContract(repository).execute(
        contract=contract,
        idempotency_key="postgres-contract-key",
    )
    replay = RegisterCryptoPaperProviderContract(repository).execute(
        contract=contract,
        idempotency_key="postgres-contract-key",
    )
    assert not registered.idempotent_replay
    assert replay.idempotent_replay
    assert repository.contract(contract.contract_id) == contract

    capability = inspect_paper_capability(contract=contract, inspected_at=NOW)
    retained = RetainCryptoPaperCapability(repository).execute(
        capability=capability,
        idempotency_key="postgres-capability-key",
    )
    assert not retained.idempotent_replay
    assert repository.capability(capability.capability_id) == capability
    assert capability.primary_state is ProviderCapabilityState.PAPER_ONLY
    assert capability.provider_requests == 0

    created = CreateCryptoPaperAccount(repository).execute(
        account_id="postgres-crypto-paper-account",
        opening_cash="1000",
        created_at=NOW,
        idempotency_key="postgres-account-key",
    )
    repeated = CreateCryptoPaperAccount(repository).execute(
        account_id="postgres-crypto-paper-account",
        opening_cash="1000",
        created_at=NOW,
        idempotency_key="postgres-account-key",
    )
    assert not created.idempotent_replay
    assert repeated.idempotent_replay
    assert repeated.value == created.value

    prior = created.value
    operation = CryptoPaperOperation.create(
        idempotency_key="postgres-state-operation-key",
        operation_type="TEST_APPEND_ONLY_SNAPSHOT",
        result_id="postgres-state-result",
        result_hash="b" * 64,
    )
    next_state = replace(
        prior,
        contracts=(contract,),
        capabilities=(capability,),
        operations=(operation,),
    ).sealed()
    assert not repository.save_state(
        state=next_state,
        prior_state_hash=prior.canonical_hash,
        operation=operation,
    )
    assert repository.state(prior.account.account_id) == next_state
    assert repository.save_state(
        state=next_state,
        prior_state_hash=prior.canonical_hash,
        operation=operation,
    )

    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(
            sa.text(
                "UPDATE apex_crypto_paper_state_snapshot "
                "SET operation_type = 'MUTATED' WHERE account_id = :account_id"
            ),
            {"account_id": prior.account.account_id},
        )


def test_crypto_paper_retained_evidence_blocks_migration_downgrade(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    CreateCryptoPaperAccount(repository).execute(
        account_id="postgres-crypto-paper-downgrade-account",
        opening_cash="100",
        created_at=NOW,
        idempotency_key="postgres-downgrade-account-key",
    )
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0061"):
            command.downgrade(_config(connection), "0060_recurring_operating_cycle")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0061_crypto_spot_paper"
        )


def test_full_crypto_paper_fixture_lifecycle_round_trips_from_postgres(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    result = run_crypto_paper_fixture_acceptance(repository)
    retained = repository.state(ACCEPTANCE_ACCOUNT_ID)
    assert retained is not None
    assert retained.canonical_hash == result["final_state_hash"]
    assert len(retained.orders) == 2
    assert len(retained.fills) == 1
    assert len(retained.positions) == 1
    assert len(retained.journals) == 1
    assert len(retained.reconciliations) == 1
    assert result["submit_replay"] is True
    assert result["fill_replay"] is True
    assert result["duplicate_fill_rejected"] is True
