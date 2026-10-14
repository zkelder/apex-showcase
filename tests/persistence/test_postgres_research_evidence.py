from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.research_evidence_postgres import (
    PostgresResearchEvidenceRepository,
)
from apex.adapters.security_reference import (
    NASDAQ_LISTED_PATH,
    parse_nasdaq_symbol_directory,
)
from apex.application.research_evidence import (
    CaptureNasdaqDirectory,
    ExecuteBroadUniverseExperiment,
    MaterializeResearchDatasetSnapshot,
    RecordResearchExperimentOutcome,
    RegisterResearchHypothesis,
    ResearchEvidenceConflict,
)
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)
RESEARCH_TABLES = {
    "research_hypothesis_version",
    "research_observation_capture",
    "research_observation",
    "research_capture_checkpoint",
    "research_dataset_snapshot",
    "research_experiment_run",
    "research_experiment_outcome",
}


def _raw() -> bytes:
    return (ROOT / "fixtures" / "broad-research" / "nasdaqlisted.txt").read_bytes()


def _parsed(received_at: datetime = NOW):
    return parse_nasdaq_symbol_directory(
        payload=_raw(),
        source_path=NASDAQ_LISTED_PATH,
        received_at=received_at,
    )


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_clean_database_is_at_current_head_with_research_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= RESEARCH_TABLES
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


def test_durable_lifecycle_resumes_after_fresh_repository_and_has_no_financial_effect(
    postgres_engine: Engine,
) -> None:
    repository = PostgresResearchEvidenceRepository(postgres_engine)
    hypothesis = RegisterResearchHypothesis(repository).execute(
        hypothesis_id="broad-current-universe-threshold",
        revision=1,
        question=(
            "Does the retained current Nasdaq directory produce at least one "
            "eligible security under CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1?"
        ),
        motivation="Falsify eligibility using one identified retained directory.",
        actor="persistence-test",
        market_scope=("US_EQUITIES", "NASDAQ_CURRENT_DIRECTORY"),
        created_at=NOW,
    )
    parsed = _parsed()
    capture = CaptureNasdaqDirectory(repository).start(
        capture_id="capture-persistence-v1",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/nasdaqlisted.txt",
        raw_payload=_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
        limitations=("PROVIDER_SHAPED_FIXTURE_EVIDENCE",),
    )
    assert capture.status is CaptureStatus.RUNNING

    restarted = PostgresResearchEvidenceRepository(postgres_engine)
    retained_raw = restarted.raw_payload(capture.capture_id)
    assert retained_raw == _raw()
    retained_capture = restarted.capture(capture.capture_id)
    assert retained_capture is not None
    reparsed = parse_nasdaq_symbol_directory(
        payload=retained_raw,
        source_path=NASDAQ_LISTED_PATH,
        received_at=retained_capture.ingestion_at,
    )
    completed = CaptureNasdaqDirectory(restarted).resume(
        capture_id=capture.capture_id,
        raw_payload=retained_raw,
        records=reparsed.records,
        resumed_at=NOW + timedelta(seconds=1),
        chunk_size=10,
    )
    assert completed.status is CaptureStatus.COMPLETED
    assert len(restarted.observations(capture.capture_id)) == 3
    assert tuple(item.next_ordinal for item in restarted.checkpoints(capture.capture_id)) == (1, 3)

    snapshot = MaterializeResearchDatasetSnapshot(restarted).execute(
        snapshot_id="snapshot-persistence-v1",
        component_capture_ids=(capture.capture_id,),
        created_at=NOW + timedelta(minutes=1),
    )
    run = ExecuteBroadUniverseExperiment(restarted).execute(
        run_id="experiment-persistence-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        minimum_eligible_count=1,
        code_revision="1b08d3e9b11606316349a779b34a414274900154",
        executed_at=NOW + timedelta(minutes=2),
    )
    outcome = RecordResearchExperimentOutcome(restarted).execute(
        outcome_id="outcome-persistence-v1",
        run_id=run.run_id,
        decision=ExperimentOutcomeDecision.REJECTED,
        decided_at=NOW + timedelta(minutes=3),
        actor="persistence-test",
        reason="No retained security passed the exact universe policy.",
    )

    fresh = PostgresResearchEvidenceRepository(postgres_engine)
    assert fresh.hypothesis_version(hypothesis.hypothesis_version_id) == hypothesis
    assert fresh.capture(capture.capture_id) == completed
    assert fresh.snapshot(snapshot.snapshot_id) == snapshot
    assert fresh.run(run.run_id) == run
    assert fresh.outcome(outcome.outcome_id) == outcome
    assert ExecuteBroadUniverseExperiment(fresh).replay(run.run_id).result_hash == run.result_hash
    # Every persisted decision explicitly carries both its research
    # eligibility and its independent execution-capability fields; neither
    # is dropped or re-derived on reload.
    reloaded_snapshot = fresh.snapshot(snapshot.snapshot_id)
    assert reloaded_snapshot is not None
    for decision in reloaded_snapshot.decisions:
        assert decision.execution_capability == "ALPACA_CAPABILITY_NOT_OBSERVED"
        assert decision.execution_reasons == ("NO_ALPACA_EVIDENCE_OBSERVED",)

    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0


def test_options_directory_raw_artifact_and_rejection_ledger_survive_a_fresh_repository(
    postgres_engine: Engine,
) -> None:
    """The Nasdaq options-directory capture reuses this exact schema (no new
    migration): its raw artifact and rejection ledger must round-trip
    through Postgres with full fidelity, the same as the Nasdaq/SEC captures
    above.
    """
    from apex.application.research_evidence import (
        CaptureNasdaqOptionsDirectory,
        option_underlying_rejection_from_observation,
    )
    from apex.domain.options_intelligence import (
        OptionUnderlyingLinkRejection,
        UnderlyingLinkOutcome,
    )

    repository = PostgresResearchEvidenceRepository(postgres_engine)
    raw = b"genuine nasdaq options directory raw bytes for persistence"
    digest = hashlib.sha256(raw).hexdigest()
    rejections = (
        OptionUnderlyingLinkRejection.create(
            raw_underlying_symbol="GHOST",
            outcome=UnderlyingLinkOutcome.UNLINKED,
            row_count=4,
            candidate_security_ids=(),
            reasons=("NO_MATCHING_RECONCILED_IDENTITY",),
            source_payload_hash=digest,
        ),
        OptionUnderlyingLinkRejection.create(
            raw_underlying_symbol="DUP A",
            outcome=UnderlyingLinkOutcome.AMBIGUOUS,
            row_count=1,
            candidate_security_ids=("security-1", "security-2"),
            reasons=("MULTIPLE_NORMALIZED_IDENTITY_MATCHES",),
            source_payload_hash=digest,
        ),
    )
    service = CaptureNasdaqOptionsDirectory(repository)
    capture = service.start(
        capture_id="options-directory-persistence-v1",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        raw_payload=raw,
        rejections=rejections,
        source_timestamp=NOW,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
    )
    while capture.status.value == "RUNNING":
        capture = service.resume(
            capture_id=capture.capture_id,
            raw_payload=raw,
            rejections=rejections,
            resumed_at=NOW + timedelta(seconds=1),
            chunk_size=1,
        )

    fresh = PostgresResearchEvidenceRepository(postgres_engine)
    assert fresh.capture(capture.capture_id) == capture
    assert fresh.raw_payload(capture.capture_id) == raw
    reloaded = {
        item.raw_underlying_symbol: item
        for item in (
            option_underlying_rejection_from_observation(observation)
            for observation in fresh.observations(capture.capture_id)
        )
    }
    assert reloaded["GHOST"].outcome is UnderlyingLinkOutcome.UNLINKED
    assert reloaded["GHOST"].row_count == 4
    assert reloaded["DUP A"].outcome is UnderlyingLinkOutcome.AMBIGUOUS
    assert set(reloaded["DUP A"].candidate_security_ids) == {"security-1", "security-2"}
    assert all(item.source_payload_hash == digest for item in reloaded.values())

    # A failed capture retains no partial raw artifact in Postgres either.
    failed = CaptureNasdaqOptionsDirectory(repository).record_failed(
        capture_id="options-directory-persistence-failed",
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        requested_at=NOW,
        recorded_at=NOW,
        reason="NASDAQ_OPTIONS_RESPONSE_TOO_LARGE",
    )
    assert fresh.raw_payload(failed.capture_id) is None
    assert fresh.capture(failed.capture_id) == failed


def test_incompatible_resume_is_atomic_and_observations_are_immutable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresResearchEvidenceRepository(postgres_engine)
    parsed = _parsed()
    capture = CaptureNasdaqDirectory(repository).start(
        capture_id="capture-conflict-v1",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/nasdaqlisted.txt",
        raw_payload=_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
    )
    changed = _raw().replace(b"ACME", b"ACMX")
    changed_records = parse_nasdaq_symbol_directory(
        payload=changed,
        source_path=NASDAQ_LISTED_PATH,
        received_at=NOW,
    ).records
    with pytest.raises(ResearchEvidenceConflict, match="CAPTURE_SOURCE_CONTENT_CONFLICT"):
        CaptureNasdaqDirectory(repository).resume(
            capture_id=capture.capture_id,
            raw_payload=changed,
            records=changed_records,
            resumed_at=NOW,
            chunk_size=1,
        )
    assert len(repository.observations(capture.capture_id)) == 1
    assert repository.capture(capture.capture_id) == capture

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE research_observation SET subject_id = 'changed' "
                "WHERE capture_id = :capture_id"
            ),
            {"capture_id": capture.capture_id},
        )


def test_retained_research_evidence_blocks_destructive_0048_downgrade(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0048_research_evidence")
    repository = PostgresResearchEvidenceRepository(postgres_engine)
    hypothesis = RegisterResearchHypothesis(repository).execute(
        hypothesis_id="downgrade-guard-hypothesis",
        revision=1,
        question="Does retained research evidence survive an unsafe downgrade attempt?",
        motivation="Prove destructive downgrade protection.",
        actor="migration-test",
        market_scope=("RESEARCH_GOVERNANCE",),
        created_at=NOW,
    )
    tables_before = tuple(sorted(sa.inspect(postgres_engine).get_table_names()))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception, match="cannot downgrade 0048 with retained research evidence"
        ):
            command.downgrade(_config(connection), "0047_acceptance_isolation")
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0048_research_evidence"
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM research_hypothesis_version "
                    "WHERE hypothesis_version_id = :version_id"
                ),
                {"version_id": hypothesis.hypothesis_version_id},
            )
            == 1
        )
    assert tuple(sorted(sa.inspect(postgres_engine).get_table_names())) == tables_before
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
