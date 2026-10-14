from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.crypto_campaign_candidate_postgres import (
    PostgresCryptoCampaignCandidateRepository,
)
from apex.application.crypto_campaign import RegisterCryptoCampaign
from apex.domain.crypto_campaign import FIXED_CAMPAIGN_ID
from apex.domain.crypto_campaign_candidate import CandidateHypothesisSpecification
from apex.domain.crypto_campaign_candidate_2 import BreakoutHypothesisSpecification
from apex.domain.research_evidence import HypothesisStatus, ResearchHypothesisVersion

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 21, tzinfo=UTC)
CANDIDATE_TABLES = {
    "crypto_campaign_candidate_hypothesis",
    "crypto_campaign_candidate_comparison",
    "crypto_campaign_candidate_protocol",
    "crypto_campaign_candidate_run",
    "crypto_campaign_candidate_outcome",
    "crypto_campaign_candidate_dossier",
    "crypto_campaign_candidate_nomination",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_candidate_hypothesis_is_durable_immutable_and_blocks_downgrade(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= CANDIDATE_TABLES
    repository = PostgresCryptoCampaignCandidateRepository(postgres_engine)
    campaign = RegisterCryptoCampaign(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="candidate-persistence-campaign",
    )
    hypothesis = ResearchHypothesisVersion.create(
        hypothesis_id="candidate-persistence-hypothesis",
        revision=1,
        question="Does the fixed monthly 200-observation candidate pass both windows?",
        motivation="Persistence proof only.",
        actor="persistence-test",
        market_scope=("BTC-USD", "AUTHORITY_NONE"),
        created_at=NOW,
        status=HypothesisStatus.REGISTERED,
    )
    specification = CandidateHypothesisSpecification.fixed(
        hypothesis, campaign_id=campaign.campaign_id
    )
    repository.save_candidate_hypothesis(hypothesis, specification)
    fresh = PostgresCryptoCampaignCandidateRepository(postgres_engine)
    assert fresh.candidate_hypothesis(hypothesis.hypothesis_version_id) == specification

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_campaign_candidate_hypothesis SET authority = 'PAPER' "
                "WHERE hypothesis_version_id = :identity"
            ),
            {"identity": hypothesis.hypothesis_version_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0053 with retained crypto campaign candidate evidence",
        ):
            command.downgrade(_config(connection), "0052_crypto_campaign")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0053_crypto_campaign_candidate_1"
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0


def test_candidate_2_reuses_generic_hypothesis_storage_without_mixing_parsers(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCryptoCampaignCandidateRepository(postgres_engine)
    campaign = RegisterCryptoCampaign(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="persistence-test",
        created_at=NOW,
        idempotency_key="candidate-2-persistence-campaign",
    )
    hypothesis = ResearchHypothesisVersion.create(
        hypothesis_id="candidate-2-persistence-hypothesis",
        revision=1,
        question="Does the stateful monthly 90/45 breakout pass both windows?",
        motivation="Generic persistence proof only.",
        actor="persistence-test",
        market_scope=("BTC-USD", "AUTHORITY_NONE"),
        created_at=NOW,
        status=HypothesisStatus.REGISTERED,
    )
    specification = BreakoutHypothesisSpecification.fixed(
        hypothesis, campaign_id=campaign.campaign_id
    )
    repository.save_breakout_hypothesis(hypothesis, specification)
    fresh = PostgresCryptoCampaignCandidateRepository(postgres_engine)
    assert fresh.breakout_hypothesis(hypothesis.hypothesis_version_id) == specification
    assert fresh.breakout_hypotheses() == (specification,)
    assert fresh.candidate_hypotheses() == ()
    assert fresh.candidate_hypothesis(hypothesis.hypothesis_version_id) is None

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_campaign_candidate_hypothesis "
                "SET authority = 'PAPER' WHERE hypothesis_version_id = :identity"
            ),
            {"identity": hypothesis.hypothesis_version_id},
        )
