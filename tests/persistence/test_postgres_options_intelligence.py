from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.options_intelligence_postgres import PostgresOptionsIntelligenceRepository
from apex.adapters.options_reference import (
    alpaca_options_capability,
    occ_public_reference_capability,
    parse_nasdaq_options_directory,
)
from apex.application.options_intelligence import (
    RecordOptionableUnderlyings,
    RecordOptionContracts,
    RecordOptionProviderCapability,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)
OPTIONS_INTELLIGENCE_TABLES = {
    "options_intelligence_capability",
    "options_intelligence_contract",
    "options_intelligence_optionable_underlying",
    "options_intelligence_shadow_proposal",
}


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _contracts():
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"ACME|N|P|09/18/2026|95.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    return parse_nasdaq_options_directory(
        payload=payload, received_at=NOW, underlying_ids={"ACME": "security-acme"}
    ).contracts


def test_clean_database_is_at_current_head_with_options_intelligence_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= OPTIONS_INTELLIGENCE_TABLES
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


def test_capability_contract_and_optionable_evidence_survive_a_fresh_repository(
    postgres_engine: Engine,
) -> None:
    repository = PostgresOptionsIntelligenceRepository(postgres_engine)
    occ = RecordOptionProviderCapability(repository).execute(
        capability=occ_public_reference_capability(checked_at=NOW), recorded_at=NOW
    )
    alpaca = RecordOptionProviderCapability(repository).execute(
        capability=alpaca_options_capability(), recorded_at=NOW
    )
    contracts = _contracts()
    retained_contracts = RecordOptionContracts(repository).execute(
        contracts=contracts, recorded_at=NOW
    )
    decisions = RecordOptionableUnderlyings(repository).execute(
        universe_security_ids=("security-acme", "security-beta"),
        contracts=contracts,
        source_provider="NASDAQ_OPTIONS_DIRECTORY",
        as_of="2026-07-29",
        recorded_at=NOW,
    )

    fresh = PostgresOptionsIntelligenceRepository(postgres_engine)
    assert set(fresh.capabilities()) == {occ, alpaca}
    assert fresh.capabilities(provider="OCC") == (occ,)
    assert set(fresh.contracts()) == set(retained_contracts)
    assert set(fresh.contracts_for_underlying("security-acme")) == set(retained_contracts)
    assert fresh.optionable_underlying("security-acme") in decisions
    acme_decision = fresh.optionable_underlying("security-acme")
    beta_decision = fresh.optionable_underlying("security-beta")
    assert acme_decision is not None and acme_decision.optionable
    assert beta_decision is not None and not beta_decision.optionable
    assert {item.canonical_security_id for item in fresh.optionable_underlyings()} == {
        "security-acme",
        "security-beta",
    }

    # Re-recording identical capability/contract evidence does not duplicate rows.
    RecordOptionProviderCapability(fresh).execute(
        capability=occ_public_reference_capability(checked_at=NOW), recorded_at=NOW
    )
    RecordOptionContracts(fresh).execute(contracts=contracts, recorded_at=NOW)
    assert len(fresh.capabilities(provider="OCC")) == 1
    assert len(fresh.contracts()) == len(retained_contracts)


def test_save_contracts_chunks_large_batches_and_deduplicates_in_batch_repeats(
    postgres_engine: Engine,
) -> None:
    """A genuine official capture can retain far more contracts than fit in
    one SQL statement's bound-parameter budget, and the same official row can
    legitimately repeat: neither may raise, drop evidence, or duplicate rows.
    """
    import apex.adapters.options_intelligence_postgres as options_intelligence_postgres_module

    repository = PostgresOptionsIntelligenceRepository(postgres_engine)
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"  # a genuine duplicate row
        b"ACME|N|P|09/18/2026|95.00|ACME|Acme Corporation|N\n"
        b"ACME|N|C|10/16/2026|110.00|ACME|Acme Corporation|N\n"
        b"ACME|N|P|10/16/2026|90.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    contracts = parse_nasdaq_options_directory(
        payload=payload, received_at=NOW, underlying_ids={"ACME": "security-acme"}
    ).contracts
    assert len(contracts) == 5  # the parser retains every row, duplicate included
    distinct_ids = {item.canonical_contract_id for item in contracts}
    assert len(distinct_ids) == 4  # exactly one pair shares an identity

    original_batch_size = options_intelligence_postgres_module._CONTRACT_BATCH_SIZE
    options_intelligence_postgres_module._CONTRACT_BATCH_SIZE = 2
    try:
        retained = RecordOptionContracts(repository).execute(contracts=contracts, recorded_at=NOW)
    finally:
        options_intelligence_postgres_module._CONTRACT_BATCH_SIZE = original_batch_size

    assert len(retained) == 5
    assert {item.canonical_contract_id for item in repository.contracts()} == distinct_ids
    assert len(repository.contracts()) == 4

    # Replaying the identical (still duplicate-containing) capture is a free,
    # non-duplicating idempotent read, chunked the same way.
    options_intelligence_postgres_module._CONTRACT_BATCH_SIZE = 2
    try:
        RecordOptionContracts(repository).execute(contracts=contracts, recorded_at=NOW)
    finally:
        options_intelligence_postgres_module._CONTRACT_BATCH_SIZE = original_batch_size
    assert len(repository.contracts()) == 4


def test_retained_options_intelligence_evidence_blocks_destructive_0075_downgrade(
    postgres_engine: Engine,
) -> None:
    repository = PostgresOptionsIntelligenceRepository(postgres_engine)
    RecordOptionProviderCapability(repository).execute(
        capability=occ_public_reference_capability(checked_at=NOW), recorded_at=NOW
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0075 with retained options_intelligence_capability evidence",
        ):
            command.downgrade(_config(connection), "0074_paper_activation")
        connection.rollback()
    # Alembic commits each revision's downgrade separately: 0076's own
    # downgrade (unrelated to this options_intelligence guard) already
    # succeeded and committed before 0075's guard fired and rolled back only
    # its own step, so the database genuinely rests at 0075 here -- not back
    # at head yet.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0075_broad_market_research"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
