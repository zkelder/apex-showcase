from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.crypto_challenger_postgres import PostgresCryptoChallengerRepository
from apex.application.crypto_challenger import RegisterMonthlyMomentumHypothesis

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 20, tzinfo=UTC)
CHALLENGER_TABLES = {
    "crypto_challenger_hypothesis",
    "crypto_challenger_protocol",
    "crypto_challenger_run",
    "crypto_challenger_outcome",
    "crypto_challenger_dossier",
    "crypto_challenger_conclusion",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_challenger_hypothesis_is_durable_immutable_and_blocks_downgrade(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= CHALLENGER_TABLES
    repository = PostgresCryptoChallengerRepository(postgres_engine)
    hypothesis, specification = RegisterMonthlyMomentumHypothesis(repository).execute(
        hypothesis_id="persistence-monthly-momentum-challenger",
        actor="persistence-test",
        created_at=NOW,
    )
    fresh = PostgresCryptoChallengerRepository(postgres_engine)
    assert fresh.hypothesis_version(hypothesis.hypothesis_version_id) == hypothesis
    assert fresh.challenger_hypothesis(hypothesis.hypothesis_version_id) == specification
    assert not specification.paper_eligible
    assert not specification.live_eligible
    assert specification.capital_authority == "NONE"
    assert specification.opportunity_authority == "NONE"
    assert specification.order_authority == "NONE"

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_challenger_hypothesis "
                "SET authority = 'PAPER' WHERE hypothesis_version_id = :identity"
            ),
            {"identity": hypothesis.hypothesis_version_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception, match="cannot downgrade 0051 with retained crypto challenger evidence"
        ):
            command.downgrade(_config(connection), "0050_crypto_dossier")
        connection.rollback()
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0051_crypto_challenger"
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
