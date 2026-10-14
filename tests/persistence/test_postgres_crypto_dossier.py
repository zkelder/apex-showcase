from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.coinbase_public import FixtureCoinbaseCandleSource
from apex.adapters.crypto_dossier_postgres import PostgresCryptoDossierRepository
from apex.application.crypto_dossier import (
    ExecuteCryptoDisjointReplication,
    MaterializeCryptoEvidenceDossier,
    RecordCryptoDossierConclusion,
    RecordCryptoReplicationOutcome,
    RegisterCryptoReplicationProtocol,
    RegisterFrozenCryptoStrategy,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    ExecuteCryptoTrendValidation,
    MaterializeCryptoHistoricalSnapshot,
    RecordCryptoExperimentOutcome,
    RegisterCryptoHypothesis,
)
from apex.domain.crypto_dossier import (
    ORIGINAL_CANONICAL_RUN_HASH,
    ORIGINAL_HYPOTHESIS_VERSION_ID,
    ORIGINAL_OUTCOME_HASH,
    ORIGINAL_OUTCOME_ID,
    ORIGINAL_RESULT_HASH,
    ORIGINAL_RUN_ID,
    ORIGINAL_SNAPSHOT_ID,
    ORIGINAL_SNAPSHOT_MANIFEST_HASH,
    REPLICATION_DATA_END,
    REPLICATION_DATA_START,
)
from apex.domain.crypto_research import FIXED_DATA_END, FIXED_DATA_START
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 18, tzinfo=UTC)
DOSSIER_TABLES = {
    "crypto_strategy_specification",
    "crypto_replication_protocol",
    "crypto_replication_run",
    "crypto_replication_outcome",
    "crypto_evidence_dossier",
    "crypto_dossier_conclusion",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _capture(
    repository: PostgresCryptoDossierRepository,
    *,
    capture_id: str,
    start: datetime,
    end: datetime,
):
    service = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource())
    capture = service.start(
        capture_id=capture_id,
        start=start,
        end=end,
        requested_at=NOW,
    )
    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            capture_id=capture_id,
            requested_at=NOW + timedelta(seconds=capture.request_count),
        )
    return capture


def _seed_original_rejection(repository: PostgresCryptoDossierRepository) -> None:
    hypothesis, specification = RegisterCryptoHypothesis(repository).execute(
        hypothesis_id="crypto-dossier-template",
        revision=1,
        actor="persistence-test",
        created_at=NOW,
    )
    capture = _capture(
        repository,
        capture_id="crypto-dossier-original-capture",
        start=FIXED_DATA_START,
        end=FIXED_DATA_END,
    )
    snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="crypto-dossier-original-snapshot",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    run = ExecuteCryptoTrendValidation(repository).execute(
        run_id="crypto-dossier-original-run",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        code_revision="a" * 40,
        executed_at=NOW,
    )
    outcome = RecordCryptoExperimentOutcome(repository).execute(
        outcome_id="crypto-dossier-original-outcome",
        run_id=run.run_id,
        actor="persistence-test",
        decided_at=NOW,
    )
    retained_hypothesis = replace(
        hypothesis,
        hypothesis_id="btc-usd-daily-trend-genuine-v1",
        hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
    )
    repository.save_hypothesis(
        retained_hypothesis,
        replace(
            specification,
            hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
        ),
    )
    repository.save_snapshot(
        replace(
            snapshot,
            snapshot_id=ORIGINAL_SNAPSHOT_ID,
            manifest_hash=ORIGINAL_SNAPSHOT_MANIFEST_HASH,
        )
    )
    repository.save_run(
        replace(
            run,
            run_id=ORIGINAL_RUN_ID,
            hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
            snapshot_id=ORIGINAL_SNAPSHOT_ID,
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            result_hash=ORIGINAL_RESULT_HASH,
            canonical_hash=ORIGINAL_CANONICAL_RUN_HASH,
        )
    )
    repository.save_outcome(
        replace(
            outcome,
            outcome_id=ORIGINAL_OUTCOME_ID,
            run_id=ORIGINAL_RUN_ID,
            decision=ExperimentOutcomeDecision.REJECTED,
            canonical_hash=ORIGINAL_OUTCOME_HASH,
            paper_eligible=False,
            live_eligible=False,
        )
    )


def test_crypto_dossier_lifecycle_is_durable_immutable_and_authority_free(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= DOSSIER_TABLES
    repository = PostgresCryptoDossierRepository(postgres_engine)
    _seed_original_rejection(repository)
    capture = _capture(
        repository,
        capture_id="crypto-dossier-replication-capture",
        start=REPLICATION_DATA_START,
        end=REPLICATION_DATA_END,
    )
    snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="crypto-dossier-replication-snapshot",
        capture_id=capture.capture_id,
        created_at=NOW + timedelta(minutes=1),
    )
    strategy = RegisterFrozenCryptoStrategy(repository).execute(
        specification_id="btc-usd-sma-20-100-rejected-v1",
        created_at=NOW + timedelta(minutes=2),
    )
    protocol = RegisterCryptoReplicationProtocol(repository).execute(
        protocol_id="btc-usd-disjoint-replication-v1",
        strategy_specification_id=strategy.specification_id,
        created_at=NOW + timedelta(minutes=3),
    )
    run = ExecuteCryptoDisjointReplication(repository).execute(
        run_id="btc-usd-disjoint-replication-fixture-v1",
        protocol_id=protocol.protocol_id,
        snapshot_id=snapshot.snapshot_id,
        code_revision="b" * 40,
        executed_at=NOW + timedelta(minutes=4),
    )
    outcome = RecordCryptoReplicationOutcome(repository).execute(
        outcome_id="btc-usd-disjoint-replication-outcome-v1",
        run_id=run.run_id,
        actor="persistence-test",
        decided_at=NOW + timedelta(minutes=5),
    )
    dossier = MaterializeCryptoEvidenceDossier(repository).execute(
        dossier_id="btc-usd-strategy-evidence-dossier-v1",
        replication_outcome_id=outcome.outcome_id,
        created_at=NOW + timedelta(minutes=6),
    )
    conclusion = RecordCryptoDossierConclusion(repository).execute(
        conclusion_id="btc-usd-strategy-dossier-conclusion-v1",
        dossier_id=dossier.dossier_id,
        actor="persistence-test",
        decided_at=NOW + timedelta(minutes=7),
    )

    fresh = PostgresCryptoDossierRepository(postgres_engine)
    assert fresh.strategy_specification(strategy.specification_id) == strategy
    assert fresh.replication_protocol(protocol.protocol_id) == protocol
    assert fresh.replication_run(run.run_id) == run
    assert fresh.replication_outcome(outcome.outcome_id) == outcome
    assert fresh.dossier(dossier.dossier_id) == dossier
    assert fresh.dossier_conclusion(conclusion.conclusion_id) == conclusion
    assert ExecuteCryptoDisjointReplication(fresh).replay(run.run_id) == run
    assert outcome.decision is ExperimentOutcomeDecision.DEFERRED
    assert not outcome.paper_eligible and not outcome.live_eligible

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_strategy_specification "
                "SET lifecycle_state = 'PROMOTED' WHERE specification_id = :identity"
            ),
            {"identity": strategy.specification_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception, match="cannot downgrade 0050 with retained crypto dossier evidence"
        ):
            command.downgrade(_config(connection), "0049_crypto_validation")
        connection.rollback()
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0050_crypto_dossier"
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
