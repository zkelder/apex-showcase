from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apex.adapters.broad_options_demo import run_broad_equity_universe_demo
from apex.adapters.research_evidence_source import (
    NasdaqDirectoryClient,
    NasdaqDirectoryClientError,
    NasdaqDirectoryHttpResponse,
    SecCompanyTickersCapabilityUnavailable,
    SecCompanyTickersClient,
    SecCompanyTickersClientError,
)
from apex.adapters.security_reference import (
    NASDAQ_LISTED_PATH,
    NASDAQ_OTHER_LISTED_PATH,
    NASDAQ_TRADER_HOST,
    SEC_DATA_HOST,
    SEC_TICKERS_PATH,
    SecurityReferenceError,
    parse_nasdaq_symbol_directory,
    parse_sec_company_tickers,
)
from apex.application.research_evidence import (
    CaptureNasdaqDirectory,
    CaptureSecCompanyTickers,
    ExecuteBroadUniverseExperiment,
    InMemoryResearchEvidenceRepository,
    MaterializeResearchDatasetSnapshot,
    RecordResearchExperimentOutcome,
    RegisterResearchHypothesis,
    ResearchEvidenceConflict,
    research_workspace,
    security_from_observation,
)
from apex.domain.research_evidence import (
    BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1,
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ResearchEvidenceError,
)
from apex.domain.security_reference import SecurityReference, reconcile_security_references
from apex.operator_api import _retained_research_workspace, app
from scripts.dev import _reported_research_evidence_class, parser

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)


def _raw() -> bytes:
    return (ROOT / "fixtures" / "broad-research" / "nasdaqlisted.txt").read_bytes()


def _parsed(*, received_at: datetime = NOW):
    return parse_nasdaq_symbol_directory(
        payload=_raw(),
        source_path=NASDAQ_LISTED_PATH,
        received_at=received_at,
    )


def _hypothesis(repository: InMemoryResearchEvidenceRepository):
    return RegisterResearchHypothesis(repository).execute(
        hypothesis_id="broad-current-universe-threshold",
        revision=1,
        question=(
            "Does the retained current Nasdaq directory produce at least one "
            "eligible security under CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1?"
        ),
        motivation="Falsify eligibility using one identified retained directory.",
        actor="fixture-test",
        market_scope=("US_EQUITIES", "NASDAQ_CURRENT_DIRECTORY"),
        created_at=NOW,
    )


def _completed_capture(
    repository: InMemoryResearchEvidenceRepository,
    *,
    capture_id: str = "capture-fixture-v1",
    evidence_class: EvidenceClass = EvidenceClass.FIXTURE,
):
    parsed = _parsed()
    return CaptureNasdaqDirectory(repository).start(
        capture_id=capture_id,
        evidence_class=evidence_class,
        source_locator=(
            "fixture:broad-research/nasdaqlisted.txt"
            if evidence_class is EvidenceClass.FIXTURE
            else f"https://{NASDAQ_TRADER_HOST}{NASDAQ_LISTED_PATH}"
        ),
        raw_payload=_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=len(parsed.records),
        limitations=("PROVIDER_SHAPED_FIXTURE_EVIDENCE",),
    )


def test_hypothesis_versions_have_explicit_identity_and_lineage() -> None:
    repository = InMemoryResearchEvidenceRepository()
    first = _hypothesis(repository)
    replay = _hypothesis(repository)
    assert replay == first
    assert first.hypothesis_id == "broad-current-universe-threshold"
    assert first.hypothesis_version_id.startswith("research_hypothesis_version_")

    second = RegisterResearchHypothesis(repository).execute(
        hypothesis_id=first.hypothesis_id,
        revision=2,
        question="Does the retained directory produce at least two eligible securities?",
        motivation=first.motivation,
        actor=first.actor,
        market_scope=first.market_scope,
        created_at=NOW + timedelta(seconds=1),
        supersedes_version_id=first.hypothesis_version_id,
    )
    assert second.hypothesis_version_id != first.hypothesis_version_id
    assert second.supersedes_version_id == first.hypothesis_version_id
    with pytest.raises(ResearchEvidenceConflict, match="HYPOTHESIS_REVISION_CONFLICT"):
        RegisterResearchHypothesis(repository).execute(
            hypothesis_id=first.hypothesis_id,
            revision=1,
            question="A materially different claim cannot reuse revision one.",
            motivation=first.motivation,
            actor=first.actor,
            market_scope=first.market_scope,
            created_at=NOW,
        )


def test_capture_checkpoints_resume_without_duplicates_and_reject_changed_source() -> None:
    repository = InMemoryResearchEvidenceRepository()
    service = CaptureNasdaqDirectory(repository)
    parsed = _parsed()
    partial = service.start(
        capture_id="capture-partial",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/nasdaqlisted.txt",
        raw_payload=_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
    )
    assert partial.status is CaptureStatus.RUNNING
    assert partial.retained_observations == 1
    assert len(repository.checkpoints(partial.capture_id)) == 1

    completed = service.resume(
        capture_id=partial.capture_id,
        raw_payload=_raw(),
        records=parsed.records,
        resumed_at=NOW + timedelta(seconds=1),
        chunk_size=10,
    )
    assert completed.status is CaptureStatus.COMPLETED
    assert len(repository.observations(partial.capture_id)) == len(parsed.records)
    assert tuple(item.ordinal for item in repository.observations(partial.capture_id)) == tuple(
        range(len(parsed.records))
    )
    replay = service.resume(
        capture_id=partial.capture_id,
        raw_payload=_raw(),
        records=parsed.records,
        resumed_at=NOW + timedelta(seconds=2),
        chunk_size=10,
    )
    assert replay.state_hash == completed.state_hash
    assert len(repository.observations(partial.capture_id)) == len(parsed.records)

    changed = _raw().replace(b"ACME", b"ACMX")
    changed_records = parse_nasdaq_symbol_directory(
        payload=changed,
        source_path=NASDAQ_LISTED_PATH,
        received_at=NOW,
    ).records
    with pytest.raises(ResearchEvidenceConflict, match="CAPTURE_SOURCE_CONTENT_CONFLICT"):
        service.resume(
            capture_id=partial.capture_id,
            raw_payload=changed,
            records=changed_records,
            resumed_at=NOW,
            chunk_size=1,
        )
    assert len(repository.observations(partial.capture_id)) == len(parsed.records)


def test_capture_retains_immutable_provenance_and_evidence_class() -> None:
    repository = InMemoryResearchEvidenceRepository()
    capture = _completed_capture(repository)
    observations = repository.observations(capture.capture_id)
    assert capture.evidence_class is EvidenceClass.FIXTURE
    assert capture.raw_sha256 == hashlib.sha256(_raw()).hexdigest()
    assert repository.raw_payload(capture.capture_id) == _raw()
    assert all(item.raw_sha256 == capture.raw_sha256 for item in observations)
    assert all(item.evidence_class is EvidenceClass.FIXTURE for item in observations)
    assert all(item.provider == "NASDAQ_TRADER" for item in observations)
    assert all(item.authority == "NONE" for item in observations)


class _Transport:
    def __init__(self, response: NasdaqDirectoryHttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, int, int]] = []

    def get(
        self,
        *,
        host: str,
        path: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> NasdaqDirectoryHttpResponse:
        self.calls.append((host, path, timeout_seconds, maximum_response_bytes))
        return self.response


def test_official_client_is_one_request_bounded_and_rejects_malformed_input() -> None:
    transport = _Transport(NasdaqDirectoryHttpResponse(200, _raw()))
    payload, parsed = NasdaqDirectoryClient(transport).capture(
        received_at=NOW,
        timeout_seconds=5,
        maximum_response_bytes=len(_raw()),
        maximum_records=10,
    )
    assert payload == _raw()
    assert len(parsed.records) == 3
    assert transport.calls == [(NASDAQ_TRADER_HOST, NASDAQ_LISTED_PATH, 5, len(_raw()))]

    oversized = _Transport(NasdaqDirectoryHttpResponse(200, _raw()))
    with pytest.raises(NasdaqDirectoryClientError, match="NASDAQ_RESPONSE_SIZE_INVALID"):
        NasdaqDirectoryClient(oversized).capture(
            received_at=NOW,
            timeout_seconds=5,
            maximum_response_bytes=len(_raw()) - 1,
        )
    malformed = _Transport(NasdaqDirectoryHttpResponse(200, b"Symbol|Security Name\nACME|Acme"))
    with pytest.raises(SecurityReferenceError):
        NasdaqDirectoryClient(malformed).capture(
            received_at=NOW,
            timeout_seconds=5,
        )


def test_other_listed_client_is_one_request_bounded_and_rejects_malformed_input() -> None:
    """otherlisted.txt reuses the identical transport/bounds as
    nasdaqlisted.txt via the explicit source_path parameter -- same one
    allowlisted host, same one-request-only, same size/record bounds."""
    other_raw = _other_listed_raw()
    transport = _Transport(NasdaqDirectoryHttpResponse(200, other_raw))
    payload, parsed = NasdaqDirectoryClient(transport).capture(
        received_at=NOW,
        timeout_seconds=5,
        source_path=NASDAQ_OTHER_LISTED_PATH,
        maximum_response_bytes=len(other_raw),
        maximum_records=10,
    )
    assert payload == other_raw
    assert len(parsed.records) == 3
    assert transport.calls == [(NASDAQ_TRADER_HOST, NASDAQ_OTHER_LISTED_PATH, 5, len(other_raw))]
    assert (
        parsed.receipt.source_locator == f"https://{NASDAQ_TRADER_HOST}{NASDAQ_OTHER_LISTED_PATH}"
    )

    oversized = _Transport(NasdaqDirectoryHttpResponse(200, other_raw))
    with pytest.raises(NasdaqDirectoryClientError, match="NASDAQ_RESPONSE_SIZE_INVALID"):
        NasdaqDirectoryClient(oversized).capture(
            received_at=NOW,
            timeout_seconds=5,
            source_path=NASDAQ_OTHER_LISTED_PATH,
            maximum_response_bytes=len(other_raw) - 1,
        )
    malformed = _Transport(
        NasdaqDirectoryHttpResponse(200, b"ACT Symbol|Security Name\nOTHR|Other")
    )
    with pytest.raises(SecurityReferenceError):
        NasdaqDirectoryClient(malformed).capture(
            received_at=NOW,
            timeout_seconds=5,
            source_path=NASDAQ_OTHER_LISTED_PATH,
        )

    # An unbounded path (not one of the two allowlisted Nasdaq Trader
    # symbol-directory files) is rejected before any transport call is made
    # to succeed -- the invalid bound check runs before dispatch.
    with pytest.raises(NasdaqDirectoryClientError, match="NASDAQ_CAPTURE_BOUNDS_INVALID"):
        NasdaqDirectoryClient(transport).capture(
            received_at=NOW, timeout_seconds=5, source_path="/dynamic/SymDir/options.txt"
        )


def test_snapshot_manifest_is_immutable_and_evidence_classes_cannot_blend() -> None:
    repository = InMemoryResearchEvidenceRepository()
    fixture = _completed_capture(repository)
    service = MaterializeResearchDatasetSnapshot(repository)
    snapshot = service.execute(
        snapshot_id="snapshot-fixture-v1",
        component_capture_ids=(fixture.capture_id,),
        created_at=NOW + timedelta(minutes=1),
    )
    replay = service.execute(
        snapshot_id=snapshot.snapshot_id,
        component_capture_ids=(fixture.capture_id,),
        created_at=NOW + timedelta(minutes=2),
    )
    assert replay.manifest_hash == snapshot.manifest_hash
    assert replay.created_at == snapshot.created_at
    assert snapshot.total_considered == 3
    # ACME and REIT are current-listed common/REIT stock with no test-issue or
    # adverse-listing exclusion; FUND is an ETF and stays excluded by type.
    # Alpaca evidence was never merged into this Nasdaq-only capture, and its
    # absence does not exclude either from the research universe.
    assert snapshot.eligible_count == 2
    assert snapshot.normalized_security_ids
    assert snapshot.source_versions == ("NASDAQ_TRADER_SYMBOL_DIRECTORY_V1",)
    eligible_symbols = {item.current_symbol for item in snapshot.decisions if item.eligible}
    assert eligible_symbols == {"ACME", "REIT"}
    assert all(
        item.execution_capability == "ALPACA_CAPABILITY_NOT_OBSERVED" for item in snapshot.decisions
    )

    genuine = CaptureNasdaqDirectory(repository).record_unavailable(
        capture_id="capture-genuine-unavailable-v1",
        source_locator=f"https://{NASDAQ_TRADER_HOST}{NASDAQ_LISTED_PATH}",
        requested_at=NOW,
        recorded_at=NOW,
        reason="TEST_ONLY_GENUINE_CAPABILITY_UNAVAILABLE",
    )
    with pytest.raises(ResearchEvidenceConflict, match="SNAPSHOT_EVIDENCE_CLASS_MIXING_PROHIBITED"):
        service.execute(
            snapshot_id="snapshot-mixed",
            component_capture_ids=(fixture.capture_id, genuine.capture_id),
            created_at=NOW,
        )


def test_provider_complete_demo_and_nasdaq_only_lifecycle_have_distinct_eligibility() -> None:
    """ "Distinct eligibility" here means distinct *execution capability* --
    research eligibility must be identical with or without Alpaca evidence.
    """
    demo = run_broad_equity_universe_demo()
    assert demo["total_considered"] == 5
    assert demo["eligible_count"] == 2
    assert set(demo["eligible_symbols"]) == {"ACME", "REIT"}
    assert demo["source_providers"] == ("NASDAQ_TRADER", "SEC_EDGAR", "ALPACA")
    # The provider-complete demo's Alpaca fixture covers every symbol.
    assert demo["execution_capability_known_count"] == 5
    assert demo["execution_capability_unknown_count"] == 0

    repository = InMemoryResearchEvidenceRepository()
    capture = _completed_capture(repository)
    snapshot = MaterializeResearchDatasetSnapshot(repository).execute(
        snapshot_id="nasdaq-only-distinction",
        component_capture_ids=(capture.capture_id,),
        created_at=NOW,
    )
    assert snapshot.total_considered == 3
    # Research eligibility is unchanged from the provider-complete case above
    # for the two symbols both fixtures share: Alpaca evidence never gates it.
    assert snapshot.eligible_count == 2
    assert {decision.current_symbol for decision in snapshot.decisions if decision.eligible} == {
        "ACME",
        "REIT",
    }
    assert all(
        "NOT_CONFIRMED_ALPACA_TRADABLE" not in decision.reasons for decision in snapshot.decisions
    )
    # What genuinely differs without Alpaca evidence is execution capability,
    # honestly reported as not observed rather than inferred either way.
    assert all(
        decision.execution_capability == "ALPACA_CAPABILITY_NOT_OBSERVED"
        for decision in snapshot.decisions
        if decision.current_symbol in {"ACME", "REIT"}
    )


def test_experiment_replays_exactly_and_retains_failure_and_outcome() -> None:
    repository = InMemoryResearchEvidenceRepository()
    hypothesis = _hypothesis(repository)
    capture = _completed_capture(repository)
    snapshot = MaterializeResearchDatasetSnapshot(repository).execute(
        snapshot_id="snapshot-fixture-v1",
        component_capture_ids=(capture.capture_id,),
        created_at=NOW,
    )
    experiments = ExecuteBroadUniverseExperiment(repository)
    run = experiments.execute(
        run_id="experiment-fixture-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        minimum_eligible_count=1,
        code_revision="1b08d3e9b11606316349a779b34a414274900154",
        executed_at=NOW + timedelta(minutes=2),
    )
    replay = experiments.replay(run.run_id)
    assert replay.result_hash == run.result_hash
    assert run.executor_version == BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1
    assert dict(run.configuration)["minimum_eligible_count"] == 1
    assert run.code_revision == "1b08d3e9b11606316349a779b34a414274900154"
    # ACME and REIT are both research-eligible from Nasdaq evidence alone
    # (see test_snapshot_manifest_is_immutable_and_evidence_classes_cannot_blend),
    # so the >=1 threshold this hypothesis registers is genuinely met.
    assert dict((item.name, item.value) for item in run.metrics)["hypothesis_supported"] == "true"
    assert run.artifacts
    assert run.authority == "NONE"

    failed = experiments.execute(
        run_id="experiment-failed-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        minimum_eligible_count=1,
        code_revision=run.code_revision,
        executed_at=NOW,
        executor_version="UNSUPPORTED_EXECUTOR",
    )
    assert failed.state is ExperimentState.FAILED
    assert failed.failure_reason == "UNSUPPORTED_EXPERIMENT_EXECUTOR_VERSION"

    outcome = RecordResearchExperimentOutcome(repository).execute(
        outcome_id="outcome-fixture-v1",
        run_id=run.run_id,
        decision=ExperimentOutcomeDecision.REJECTED,
        decided_at=NOW + timedelta(minutes=3),
        actor="fixture-test",
        reason="The retained eligible count was below the registered threshold.",
    )
    assert outcome.reason.startswith("The retained eligible count")
    assert outcome.evidence_hashes
    assert outcome.paper_eligible is False
    assert outcome.live_eligible is False
    assert outcome.authority == "NONE"
    assert _reported_research_evidence_class(outcome, repository) is EvidenceClass.FIXTURE
    with pytest.raises(ResearchEvidenceError, match="FIXTURE_CANNOT_BECOME_PROMOTION_CANDIDATE"):
        RecordResearchExperimentOutcome(repository).execute(
            outcome_id="outcome-fixture-promotion",
            run_id=run.run_id,
            decision=ExperimentOutcomeDecision.PROMOTION_CANDIDATE,
            decided_at=NOW,
            actor="fixture-test",
            reason="Fixture evidence cannot pass this fail-closed boundary.",
        )


def test_workspace_and_exact_id_command_and_api_contracts() -> None:
    empty = InMemoryResearchEvidenceRepository()
    assert research_workspace(empty)["observation_count"] == 0
    assert _retained_research_workspace(empty)["runs"] == ()

    populated = InMemoryResearchEvidenceRepository()
    hypothesis = _hypothesis(populated)
    capture = _completed_capture(populated)
    workspace = _retained_research_workspace(populated)
    assert workspace["hypotheses"] == (hypothesis,)
    assert workspace["captures"] == (capture,)
    assert workspace["observation_count"] == 3
    assert workspace["orders"] == 0
    assert workspace["positions"] == 0
    assert workspace["reservations"] == 0
    assert workspace["journals"] == 0
    assert workspace["broker_writes"] == 0
    assert (
        _reported_research_evidence_class({"capture": capture}, populated) is EvidenceClass.FIXTURE
    )
    assert _reported_research_evidence_class(workspace, populated) == ["FIXTURE"]

    parsed = parser().parse_args(
        [
            "research-experiment-run",
            "--run-id",
            "run-exact",
            "--hypothesis-version-id",
            "hypothesis-version-exact",
            "--snapshot-id",
            "snapshot-exact",
            "--minimum-eligible-count",
            "1",
        ]
    )
    assert parsed.run_id == "run-exact"
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "research-experiment-run",
                "--hypothesis-version-id",
                "hypothesis-version-exact",
                "--snapshot-id",
                "snapshot-exact",
                "--minimum-eligible-count",
                "1",
            ]
        )

    paths = {route.path for route in app.routes}
    assert "/api/v1/research/hypotheses/{hypothesis_version_id}" in paths
    assert "/api/v1/research/captures/{capture_id}" in paths
    assert "/api/v1/research/snapshots/{snapshot_id}" in paths
    assert "/api/v1/research/runs/{run_id}" in paths
    assert "/api/v1/research/outcomes/{outcome_id}" in paths


def _sec_raw() -> bytes:
    return (ROOT / "fixtures" / "broad-research" / "sec_company_tickers.json").read_bytes()


def _sec_parsed(*, received_at: datetime = NOW):
    return parse_sec_company_tickers(
        payload=_sec_raw(), received_at=received_at, source_timestamp=received_at
    )


def _other_listed_raw() -> bytes:
    # Genuine otherlisted.txt column order and required fields (ACT Symbol,
    # Security Name, Exchange, Test Issue, Round Lot Size, ETF) -- a security
    # primarily listed away from Nasdaq itself, unlike nasdaqlisted.txt.
    return (
        b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
        b"NASDAQ Symbol\n"
        b"OTHR|Other Exchange Corporation Common Stock|N|OTHR|N|100|N|OTHR\n"
        b"BF.A|Brown Forman Corporation Class A Common Stock|N|BF.A|N|100|N|BF.A\n"
        b"AMEX|Amex Listed Corporation Common Stock|A|AMEX|N|100|N|AMEX\n"
        b"File Creation Time: 0729202612:30|||||||\n"
    )


def _other_listed_parsed(*, received_at: datetime = NOW):
    return parse_nasdaq_symbol_directory(
        payload=_other_listed_raw(),
        source_path=NASDAQ_OTHER_LISTED_PATH,
        received_at=received_at,
    )


def _completed_other_listed_capture(
    repository: InMemoryResearchEvidenceRepository,
    *,
    capture_id: str = "nasdaq-other-listed-fixture-v1",
):
    parsed = _other_listed_parsed()
    return CaptureNasdaqDirectory(repository).start(
        capture_id=capture_id,
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/otherlisted.txt",
        raw_payload=_other_listed_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=len(parsed.records),
        limitations=("PROVIDER_SHAPED_FIXTURE_EVIDENCE",),
    )


def test_other_listed_capture_reuses_the_identical_generic_capture_lifecycle() -> None:
    """otherlisted.txt reuses CaptureNasdaqDirectory exactly -- same provider,
    parser version, checkpoint/resume/replay behavior, and authority as
    nasdaqlisted.txt; only the parsed records and source_locator differ."""
    repository = InMemoryResearchEvidenceRepository()
    capture = _completed_other_listed_capture(repository)
    assert capture.status is CaptureStatus.COMPLETED
    assert capture.provider == "NASDAQ_TRADER"
    assert capture.parser_version == "APEX_NASDAQ_DIRECTORY_PARSER_V1"
    assert capture.raw_sha256 == hashlib.sha256(_other_listed_raw()).hexdigest()
    assert capture.retained_observations == 3
    observations = repository.observations(capture.capture_id)
    assert len(observations) == 3
    assert all(item.authority == "NONE" for item in observations)
    assert {item.subject_id for item in observations} == {
        security_from_observation(item).canonical_security_id for item in observations
    }

    # Deterministic replay: resuming an already-COMPLETED capture with the
    # identical payload is a free, non-duplicating no-op.
    replay = CaptureNasdaqDirectory(repository).resume(
        capture_id=capture.capture_id,
        raw_payload=_other_listed_raw(),
        records=_other_listed_parsed().records,
        resumed_at=NOW + timedelta(seconds=1),
        chunk_size=10,
    )
    assert replay.state_hash == capture.state_hash
    assert len(repository.observations(capture.capture_id)) == 3


def test_other_listed_checkpoints_resume_without_duplicates() -> None:
    repository = InMemoryResearchEvidenceRepository()
    service = CaptureNasdaqDirectory(repository)
    parsed = _other_listed_parsed()
    partial = service.start(
        capture_id="other-listed-partial",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/otherlisted.txt",
        raw_payload=_other_listed_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
    )
    assert partial.status is CaptureStatus.RUNNING
    assert partial.retained_observations == 1
    assert len(repository.checkpoints(partial.capture_id)) == 1

    completed = service.resume(
        capture_id=partial.capture_id,
        raw_payload=_other_listed_raw(),
        records=parsed.records,
        resumed_at=NOW + timedelta(seconds=1),
        chunk_size=10,
    )
    assert completed.status is CaptureStatus.COMPLETED
    assert len(repository.observations(partial.capture_id)) == len(parsed.records)

    changed = _other_listed_raw().replace(b"OTHR", b"OTHX")
    changed_records = parse_nasdaq_symbol_directory(
        payload=changed, source_path=NASDAQ_OTHER_LISTED_PATH, received_at=NOW
    ).records
    with pytest.raises(ResearchEvidenceConflict, match="CAPTURE_SOURCE_CONTENT_CONFLICT"):
        service.resume(
            capture_id=partial.capture_id,
            raw_payload=changed,
            records=changed_records,
            resumed_at=NOW,
            chunk_size=1,
        )


def test_sec_capture_reuses_the_identical_generic_capture_checkpoint_lifecycle() -> None:
    repository = InMemoryResearchEvidenceRepository()
    service = CaptureSecCompanyTickers(repository)
    parsed = _sec_parsed()
    partial = service.start(
        capture_id="sec-capture-partial",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/sec_company_tickers.json",
        raw_payload=_sec_raw(),
        records=parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
    )
    assert partial.status is CaptureStatus.RUNNING
    assert partial.provider == "SEC_EDGAR"
    assert partial.retained_observations == 1
    assert len(repository.checkpoints(partial.capture_id)) == 1

    completed = service.resume(
        capture_id=partial.capture_id,
        raw_payload=_sec_raw(),
        records=parsed.records,
        resumed_at=NOW + timedelta(seconds=1),
        chunk_size=10,
    )
    assert completed.status is CaptureStatus.COMPLETED
    assert len(repository.observations(partial.capture_id)) == len(parsed.records)
    replay = service.resume(
        capture_id=partial.capture_id,
        raw_payload=_sec_raw(),
        records=parsed.records,
        resumed_at=NOW + timedelta(seconds=2),
        chunk_size=10,
    )
    assert replay.state_hash == completed.state_hash

    unavailable = CaptureSecCompanyTickers(repository).record_unavailable(
        capture_id="sec-capture-unavailable",
        source_locator=f"https://{SEC_DATA_HOST}{SEC_TICKERS_PATH}",
        requested_at=NOW,
        recorded_at=NOW,
        reason="TEST_ONLY_GENUINE_CAPABILITY_UNAVAILABLE",
    )
    assert unavailable.status is CaptureStatus.CAPABILITY_UNAVAILABLE
    assert unavailable.provider == "SEC_EDGAR"


def test_nasdaq_and_sec_captures_reconcile_into_one_confirmed_identity() -> None:
    repository = InMemoryResearchEvidenceRepository()
    nasdaq_capture = _completed_capture(repository, capture_id="nasdaq-for-reconciliation")
    sec_capture = CaptureSecCompanyTickers(repository).start(
        capture_id="sec-for-reconciliation",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/sec_company_tickers.json",
        raw_payload=_sec_raw(),
        records=_sec_parsed().records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=10,
    )
    records = tuple(
        security_from_observation(observation)
        for capture in (nasdaq_capture, sec_capture)
        for observation in repository.observations(capture.capture_id)
    )
    reconciled = reconcile_security_references(records)
    acme = next(item for item in reconciled if item.current_symbol == "ACME")
    assert acme.cik == "0000001001"
    assert "PROVIDER_ONLY" not in [item.value for item in acme.classifications]
    assert not acme.requires_review


def test_nasdaq_other_listed_and_sec_reconcile_across_all_three_sources() -> None:
    """An otherlisted.txt-only common stock must reconcile with full listing
    evidence and remain research-eligible -- it must never be treated as
    lacking official active/listing evidence merely for being absent from
    nasdaqlisted.txt."""
    from apex.domain.security_reference import (
        CurrentEquityUniversePolicy,
        create_current_equity_universe_snapshot,
    )

    repository = InMemoryResearchEvidenceRepository()
    nasdaq_capture = _completed_capture(repository, capture_id="nasdaq-3way")
    other_listed_capture = _completed_other_listed_capture(repository, capture_id="other-3way")
    sec_capture = CaptureSecCompanyTickers(repository).start(
        capture_id="sec-3way",
        evidence_class=EvidenceClass.FIXTURE,
        source_locator="fixture:broad-research/sec_company_tickers.json",
        raw_payload=_sec_raw(),
        records=_sec_parsed().records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=10,
    )
    records = tuple(
        security_from_observation(observation)
        for capture in (nasdaq_capture, other_listed_capture, sec_capture)
        for observation in repository.observations(capture.capture_id)
    )
    # Duplicate/conflicting-ticker handling: exact-symbol grouping, not fuzzy
    # matching, so nasdaqlisted's and otherlisted's disjoint symbol sets
    # never spuriously collide, and each reconciled identity's source count
    # reflects only genuine same-symbol evidence.
    reconciled = reconcile_security_references(records)
    by_symbol = {item.current_symbol: item for item in reconciled}
    assert set(by_symbol) >= {"ACME", "REIT", "FUND", "OTHR", "BF.A", "AMEX"}

    other = by_symbol["OTHR"]
    assert other.source_record_ids  # official listing evidence is retained
    assert other.listing_exchange == "XNYS"
    assert other.active.value == "YES"
    assert other.listing_status == "CURRENT_LISTED"
    assert not other.requires_review
    assert "PROVIDER_ONLY" in [item.value for item in other.classifications]

    class_share = by_symbol["BF.A"]
    assert class_share.listing_exchange == "XNYS"
    assert class_share.security_type.value == "COMMON_STOCK"

    amex_listed = by_symbol["AMEX"]
    assert amex_listed.listing_exchange == "XASE"

    # Full universe build: an otherlisted-only common stock must be
    # research-eligible on the same terms as a nasdaqlisted one -- absence
    # from nasdaqlisted.txt is never itself an exclusion reason.
    snapshot = create_current_equity_universe_snapshot(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=tuple(sorted({item.source_receipt_id for item in records})),
        as_of_date=NOW.date(),
        created_at=NOW,
    )
    other_decision = next(item for item in snapshot.decisions if item.current_symbol == "OTHR")
    assert other_decision.research_eligible
    assert other_decision.research_reasons == ()
    # Execution capability stays independent of listing source: absent
    # Alpaca evidence is unknown, never a research exclusion either way.
    assert other_decision.execution_capability.value == "ALPACA_CAPABILITY_NOT_OBSERVED"


class _SecTransport:
    def __init__(self, response: NasdaqDirectoryHttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str, int, int]] = []

    def get(
        self,
        *,
        host: str,
        path: str,
        user_agent: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> NasdaqDirectoryHttpResponse:
        self.calls.append((host, path, user_agent, timeout_seconds, maximum_response_bytes))
        return self.response


def test_sec_official_client_is_one_request_bounded_and_requires_identifying_agent() -> None:
    transport = _SecTransport(NasdaqDirectoryHttpResponse(200, _sec_raw()))
    payload, parsed = SecCompanyTickersClient(transport).capture(
        user_agent="ApexResearch/1.0 research@example.com",
        received_at=NOW,
        timeout_seconds=5,
        maximum_response_bytes=len(_sec_raw()),
    )
    assert payload == _sec_raw()
    assert len(parsed.records) == 2
    assert transport.calls == [
        (
            SEC_DATA_HOST,
            SEC_TICKERS_PATH,
            "ApexResearch/1.0 research@example.com",
            5,
            len(_sec_raw()),
        )
    ]

    with pytest.raises(SecurityReferenceError, match="SEC_IDENTIFYING_USER_AGENT_REQUIRED"):
        SecCompanyTickersClient(transport).capture(
            user_agent="anonymous", received_at=NOW, timeout_seconds=5
        )

    oversized = _SecTransport(NasdaqDirectoryHttpResponse(200, _sec_raw()))
    with pytest.raises(SecCompanyTickersClientError, match="SEC_RESPONSE_SIZE_INVALID"):
        SecCompanyTickersClient(oversized).capture(
            user_agent="ApexResearch/1.0 research@example.com",
            received_at=NOW,
            timeout_seconds=5,
            maximum_response_bytes=len(_sec_raw()) - 1,
        )

    unavailable = _SecTransport(NasdaqDirectoryHttpResponse(503, b""))
    with pytest.raises(SecCompanyTickersCapabilityUnavailable, match="SEC_PUBLIC_SOURCE_HTTP_503"):
        SecCompanyTickersClient(unavailable).capture(
            user_agent="ApexResearch/1.0 research@example.com", received_at=NOW, timeout_seconds=5
        )


def _option_rejection(symbol: str, raw_payload: bytes, *, row_count: int = 1):
    from apex.domain.options_intelligence import (
        OptionUnderlyingLinkRejection,
        UnderlyingLinkOutcome,
    )

    return OptionUnderlyingLinkRejection.create(
        raw_underlying_symbol=symbol,
        outcome=UnderlyingLinkOutcome.UNLINKED,
        row_count=row_count,
        candidate_security_ids=(),
        reasons=("NO_MATCHING_RECONCILED_IDENTITY",),
        source_payload_hash=hashlib.sha256(raw_payload).hexdigest(),
    )


def _drain_options(service, capture, raw_payload, rejections, chunk_size):
    from apex.domain.research_evidence import CaptureStatus

    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            capture_id=capture.capture_id,
            raw_payload=raw_payload,
            rejections=rejections,
            resumed_at=NOW,
            chunk_size=chunk_size,
        )
    return capture


def test_options_directory_capture_retains_raw_artifact_and_rejection_ledger() -> None:
    from apex.application.research_evidence import (
        CaptureNasdaqOptionsDirectory,
        option_underlying_rejection_from_observation,
    )
    from apex.domain.research_evidence import CaptureStatus

    repository = InMemoryResearchEvidenceRepository()
    raw = b"genuine options directory raw bytes"
    rejections = (
        _option_rejection("GHOST", raw, row_count=3),
        _option_rejection("NOWHERE", raw, row_count=1),
    )
    service = CaptureNasdaqOptionsDirectory(repository)
    capture = service.start(
        capture_id="options-directory-2026-08-06",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        raw_payload=raw,
        rejections=rejections,
        source_timestamp=NOW,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1,
    )
    capture = _drain_options(service, capture, raw, rejections, 1)

    assert capture.status is CaptureStatus.COMPLETED
    assert capture.byte_count == len(raw)
    assert capture.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert capture.retained_observations == 2
    # The raw artifact is retained and exactly reproducible.
    assert repository.raw_payload(capture.capture_id) == raw

    rebuilt = {
        item.raw_underlying_symbol: item
        for item in (
            option_underlying_rejection_from_observation(observation)
            for observation in repository.observations(capture.capture_id)
        )
    }
    assert rebuilt["GHOST"].row_count == 3
    assert rebuilt["NOWHERE"].row_count == 1
    assert all(item.source_payload_hash == capture.raw_sha256 for item in rebuilt.values())


def test_options_directory_capture_with_zero_rejections_completes_immediately() -> None:
    from apex.application.research_evidence import CaptureNasdaqOptionsDirectory
    from apex.domain.research_evidence import CaptureStatus

    repository = InMemoryResearchEvidenceRepository()
    raw = b"a fully linked options directory payload"
    capture = CaptureNasdaqOptionsDirectory(repository).start(
        capture_id="options-directory-clean-2026-08-06",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        raw_payload=raw,
        rejections=(),
        source_timestamp=NOW,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=1000,
    )
    assert capture.status is CaptureStatus.COMPLETED
    assert capture.expected_observations == 0
    assert capture.retained_observations == 0
    assert repository.raw_payload(capture.capture_id) == raw


def test_options_directory_capture_replay_is_deterministic() -> None:
    from apex.application.research_evidence import CaptureNasdaqOptionsDirectory

    repository = InMemoryResearchEvidenceRepository()
    raw = b"deterministic replay payload"
    rejections = (_option_rejection("GHOST", raw),)
    service = CaptureNasdaqOptionsDirectory(repository)
    first = _drain_options(
        service,
        service.start(
            capture_id="options-directory-replay",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
            raw_payload=raw,
            rejections=rejections,
            source_timestamp=NOW,
            requested_at=NOW,
            ingested_at=NOW,
            chunk_size=1,
        ),
        raw,
        rejections,
        1,
    )
    replay = service.start(
        capture_id="options-directory-replay",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        raw_payload=raw,
        rejections=rejections,
        source_timestamp=NOW,
        requested_at=NOW,
        ingested_at=NOW + timedelta(minutes=5),
        chunk_size=1,
    )
    assert replay.state_hash == first.state_hash
    assert replay.capture_id == first.capture_id
    assert len(repository.observations(first.capture_id)) == 1


def test_options_directory_capture_rejects_conflicting_content_replay() -> None:
    from apex.application.research_evidence import CaptureNasdaqOptionsDirectory

    repository = InMemoryResearchEvidenceRepository()
    raw = b"first genuine payload"
    rejections = (_option_rejection("GHOST", raw),)
    service = CaptureNasdaqOptionsDirectory(repository)
    _drain_options(
        service,
        service.start(
            capture_id="options-directory-conflict",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
            raw_payload=raw,
            rejections=rejections,
            source_timestamp=NOW,
            requested_at=NOW,
            ingested_at=NOW,
            chunk_size=1,
        ),
        raw,
        rejections,
        1,
    )
    changed_raw = b"a genuinely different payload"
    changed_rejections = (_option_rejection("GHOST", changed_raw),)
    with pytest.raises(ResearchEvidenceConflict, match="CAPTURE_SOURCE_CONTENT_CONFLICT"):
        service.start(
            capture_id="options-directory-conflict",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
            raw_payload=changed_raw,
            rejections=changed_rejections,
            source_timestamp=NOW,
            requested_at=NOW,
            ingested_at=NOW,
            chunk_size=1,
        )
    # The conflicting attempt never overwrote the originally retained artifact.
    assert repository.raw_payload("options-directory-conflict") == raw


def test_options_directory_capture_failure_states_retain_no_partial_raw_artifact() -> None:
    """ "Partial write cleanup" in this design means there is never a partial
    write to clean up: a capture either succeeds with the complete validated
    artifact, or a terminal failure state is recorded with zero bytes.
    """
    from apex.application.research_evidence import CaptureNasdaqOptionsDirectory
    from apex.domain.research_evidence import CaptureStatus

    repository = InMemoryResearchEvidenceRepository()
    service = CaptureNasdaqOptionsDirectory(repository)

    unavailable = service.record_unavailable(
        capture_id="options-directory-unavailable",
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        requested_at=NOW,
        recorded_at=NOW,
        reason="NASDAQ_OPTIONS_SOURCE_UNAVAILABLE:ConnectionError",
    )
    assert unavailable.status is CaptureStatus.CAPABILITY_UNAVAILABLE
    assert unavailable.byte_count == 0
    assert unavailable.retained_observations == 0
    assert repository.raw_payload(unavailable.capture_id) is None

    failed = service.record_failed(
        capture_id="options-directory-failed",
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        requested_at=NOW,
        recorded_at=NOW,
        reason="NASDAQ_OPTIONS_RESPONSE_TOO_LARGE",
    )
    assert failed.status is CaptureStatus.FAILED
    assert failed.byte_count == 0
    assert repository.raw_payload(failed.capture_id) is None
    # A capture recorded as an honest failure never becomes a later
    # "successful partial snapshot": completing it afterwards with genuine
    # content is treated as a fresh capture, not silently merged.
    assert repository.capture("options-directory-failed") == failed


class _EmptySecurityReferenceStub:
    """The minimal surface _broad_market_reference_summary reads from a
    security-reference repository. universe_evidence() is a Postgres-only
    concept (legacy-schema compatibility bookkeeping) that
    InMemorySecurityReferenceRepository does not implement, so this focused
    stub isolates the new capture-disambiguation/coverage logic under test
    from that unrelated, already-separately-tested machinery."""

    def universe_evidence(self) -> tuple[object, ...]:
        return ()

    def identities_requiring_review(self) -> tuple[object, ...]:
        return ()


class _EmptyOptionsIntelligenceStub:
    def optionable_underlyings(self) -> tuple[object, ...]:
        return ()

    def contract_count(self) -> int:
        return 0


class _FixedOptionableUnderlyingsStub:
    """Mimics exactly the genuine Postgres behavior this regression test
    guards: optionable_underlyings() retains one row per canonical_hash ever
    saved (not deduplicated across universe rebuilds), unlike
    InMemoryOptionsIntelligenceRepository, which happens to dedupe by
    canonical_security_id and so cannot reproduce the real accumulation."""

    def __init__(self, decisions: tuple[object, ...]) -> None:
        self._decisions = decisions

    def optionable_underlyings(self) -> tuple[object, ...]:
        return self._decisions

    def contract_count(self) -> int:
        return 0


def _universe_evidence_stub(snapshot: object) -> object:
    from apex.adapters.security_reference_postgres import SCHEMA_COMPATIBILITY_CURRENT

    class _Evidence:
        schema_compatibility = SCHEMA_COMPATIBILITY_CURRENT

        def __init__(self, snapshot: object) -> None:
            self.snapshot = snapshot
            self.universe_id = snapshot.universe_id  # type: ignore[attr-defined]
            self.idempotency_key = "stub"
            self.payload_schema_marker = "current"
            self.compatibility_blocker = None

    class _Repo:
        def universe_evidence(self) -> tuple[object, ...]:
            return (_Evidence(snapshot),)

        def identities_requiring_review(self) -> tuple[object, ...]:
            return ()

    return _Repo()


def test_broad_market_reference_summary_scopes_optionable_count_to_latest_universe() -> None:
    """optionable_underlyings() retains a decision per canonical_hash ever
    recorded, across every universe build -- not just the latest one -- and
    a security's canonical_security_id is not stable across rebuilds (it
    depends on which source record is "preferred," which can shift as more
    listing sources are integrated). Real expanded-universe evidence
    (2,789 of 11,657 previously-existing symbols got a new
    canonical_security_id once otherlisted.txt entered reconciliation)
    exposed exactly this: an unscoped sum silently blended a superseded
    universe's decisions into an otherwise latest-universe-scoped summary.
    """
    from datetime import date

    from apex.domain.options_intelligence import OptionableUnderlyingDecision
    from apex.domain.security_reference import (
        CurrentEquityUniversePolicy,
        create_current_equity_universe_snapshot,
        reconcile_security_references,
    )
    from apex.operator_api import _broad_market_reference_summary

    # The latest universe has exactly two securities.
    security_a_new = _reference_for_test("A", symbol="A")
    security_b = _reference_for_test("B", symbol="B")
    reconciled = reconcile_security_references((security_a_new, security_b))
    latest_snapshot = create_current_equity_universe_snapshot(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=(),
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
    )
    a_new_id = next(
        item.canonical_security_id
        for item in latest_snapshot.decisions
        if item.current_symbol == "A"
    )
    b_id = next(
        item.canonical_security_id
        for item in latest_snapshot.decisions
        if item.current_symbol == "B"
    )
    security_repo = _universe_evidence_stub(latest_snapshot)

    def _decision(security_id: str, *, optionable: bool) -> OptionableUnderlyingDecision:
        return OptionableUnderlyingDecision(
            canonical_security_id=security_id,
            optionable=optionable,
            contract_count=1 if optionable else 0,
            distinct_expirations=1 if optionable else 0,
            source_provider="NASDAQ_OPTIONS_DIRECTORY",
            reasons=(),
            reference_receipt_ids=(),
            canonical_hash="hash-" + security_id,
        )

    # A superseded decision from an OLD universe build, under "A"'s OLD
    # (no-longer-current) canonical_security_id, plus the two CURRENT
    # decisions under the latest universe's own identities.
    options_repo = _FixedOptionableUnderlyingsStub(
        (
            _decision("security-a-old-superseded-id", optionable=True),
            _decision(a_new_id, optionable=True),
            _decision(b_id, optionable=False),
        )
    )

    summary = _broad_market_reference_summary(security_repo, None, options_repo, None)
    # Exactly the two current-universe decisions -- the superseded one must
    # never be counted, whether or not it happens to still say optionable.
    assert summary["underlyings_considered"] == 2
    assert summary["optionable_underlying_count"] == 1


def _reference_for_test(receipt_suffix: str, *, symbol: str) -> SecurityReference:
    from apex.domain.security_reference import EvidenceState, SecurityType

    return SecurityReference.create(
        current_symbol=symbol,
        provider_symbol=symbol,
        issuer_name=f"{symbol} Corporation Common Stock",
        listing_exchange="XNYS",
        listing_category="N",
        security_type=SecurityType.COMMON_STOCK,
        common_stock=EvidenceState.YES,
        etf=EvidenceState.NO,
        adr=EvidenceState.NO,
        reit=EvidenceState.NO,
        test_issue=EvidenceState.NO,
        listing_status="CURRENT_LISTED",
        round_lot_size=100,
        tradable=EvidenceState.UNKNOWN,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=EvidenceState.YES,
        cik=None,
        source_provider="NASDAQ_TRADER",
        source_receipt_id=f"receipt-{receipt_suffix}",
        source_timestamp=NOW,
        source_payload_hash="0" * 64,
    )


def test_broad_market_reference_summary_reports_listing_source_coverage() -> None:
    """_broad_market_reference_summary (the Total-Market-Context-spirited
    broad-market reference summary shared by the Operator Console route and
    the broad-equity workspace route) must expose nasdaqlisted/otherlisted/
    SEC capture state separately and derive an honest coverage signal --
    Nasdaq-only until otherlisted.txt is also genuinely captured."""
    from apex.operator_api import _broad_market_reference_summary

    research_repo = InMemoryResearchEvidenceRepository()
    security_repo = _EmptySecurityReferenceStub()
    options_repo = _EmptyOptionsIntelligenceStub()

    # Genuine-shaped (not fixture-shaped) source_locator: only that exact
    # https://www.nasdaqtrader.com/... URL is what the real capture path
    # ever produces, and it is what the summary's disambiguation filters on.
    nasdaq_capture = _completed_capture(
        research_repo,
        capture_id="broad-equity-nasdaq-2026-08-06",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
    )
    nasdaq_only = _broad_market_reference_summary(security_repo, None, options_repo, research_repo)
    assert nasdaq_only["listing_source_coverage"] == "NASDAQ_ONLY"
    assert nasdaq_only["nasdaq_listed_source"]["capture_id"] == nasdaq_capture.capture_id
    assert nasdaq_only["nasdaq_listed_source"]["retained_observations"] == 3
    assert nasdaq_only["nasdaq_other_listed_source"]["capture_id"] is None
    assert nasdaq_only["sec_company_tickers_source"]["capture_id"] is None
    assert nasdaq_only["authority"] == "NONE"

    # Genuinely add the otherlisted.txt capture too -- coverage flips to
    # complete.
    other_parsed = _other_listed_parsed()
    other_capture = CaptureNasdaqDirectory(research_repo).start(
        capture_id="broad-equity-nasdaq-other-2026-08-06",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator=f"https://{NASDAQ_TRADER_HOST}{NASDAQ_OTHER_LISTED_PATH}",
        raw_payload=_other_listed_raw(),
        records=other_parsed.records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=len(other_parsed.records),
    )
    complete = _broad_market_reference_summary(security_repo, None, options_repo, research_repo)
    assert complete["listing_source_coverage"] == "NASDAQ_AND_OTHER_LISTED_COMPLETE"
    assert complete["nasdaq_other_listed_source"]["capture_id"] == other_capture.capture_id
    assert complete["nasdaq_other_listed_source"]["retained_observations"] == 3

    # SEC-only (neither Nasdaq source captured) is a real, if unlikely,
    # intermediate state and must not crash or silently claim coverage.
    sec_only_repo = InMemoryResearchEvidenceRepository()
    CaptureSecCompanyTickers(sec_only_repo).start(
        capture_id="broad-equity-sec-2026-08-06",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator=f"https://{SEC_DATA_HOST}{SEC_TICKERS_PATH}",
        raw_payload=_sec_raw(),
        records=_sec_parsed().records,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=10,
    )
    sec_only = _broad_market_reference_summary(security_repo, None, options_repo, sec_only_repo)
    assert sec_only["listing_source_coverage"] == "NO_LISTING_SOURCE_CAPTURED"
    assert sec_only["nasdaq_listed_source"]["capture_id"] is None
    assert sec_only["nasdaq_other_listed_source"]["capture_id"] is None
    assert sec_only["sec_company_tickers_source"]["capture_id"] == "broad-equity-sec-2026-08-06"


def test_latest_completed_capture_selects_most_recent_per_provider() -> None:
    from apex.application.research_evidence import (
        NASDAQ_OPTIONS_PROVIDER,
        CaptureNasdaqOptionsDirectory,
        latest_completed_capture,
    )

    repository = InMemoryResearchEvidenceRepository()
    assert latest_completed_capture(repository.captures(), NASDAQ_OPTIONS_PROVIDER) is None
    service = CaptureNasdaqOptionsDirectory(repository)
    first_raw = b"day one payload"
    service.start(
        capture_id="options-directory-2026-08-05",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        raw_payload=first_raw,
        rejections=(),
        source_timestamp=NOW,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=10,
    )
    second_raw = b"day two payload"
    second = service.start(
        capture_id="options-directory-2026-08-06",
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/options.txt",
        raw_payload=second_raw,
        rejections=(),
        source_timestamp=NOW,
        requested_at=NOW,
        ingested_at=NOW,
        chunk_size=10,
    )
    latest = latest_completed_capture(repository.captures(), NASDAQ_OPTIONS_PROVIDER)
    assert latest is not None
    assert latest.capture_id == second.capture_id == "options-directory-2026-08-06"


def test_latest_completed_capture_disambiguates_nasdaq_and_other_listed_sources() -> None:
    """nasdaqlisted.txt and otherlisted.txt both report
    provider == "NASDAQ_TRADER", so selecting "latest by provider" alone
    would silently return whichever of the two sorts last by capture_id --
    not necessarily the caller's intended source. source_locator must
    disambiguate them exactly, matching the real deterministic capture_id
    scheme (mirrors "broad-equity-nasdaq-{date}" / "broad-equity-nasdaq-
    other-{date}", where "other" sorts after a plain date lexicographically).
    """
    from apex.application.research_evidence import latest_completed_capture

    repository = InMemoryResearchEvidenceRepository()
    nasdaq_capture = _completed_capture(repository, capture_id="broad-equity-nasdaq-2026-08-06")
    other_capture = _completed_other_listed_capture(
        repository, capture_id="broad-equity-nasdaq-other-2026-08-06"
    )
    assert other_capture.capture_id > nasdaq_capture.capture_id  # confirms the trap exists

    ambiguous = latest_completed_capture(repository.captures(), "NASDAQ_TRADER")
    assert ambiguous is not None and ambiguous.capture_id == other_capture.capture_id

    resolved_nasdaq = latest_completed_capture(
        repository.captures(),
        "NASDAQ_TRADER",
        source_locator="fixture:broad-research/nasdaqlisted.txt",
    )
    resolved_other = latest_completed_capture(
        repository.captures(),
        "NASDAQ_TRADER",
        source_locator="fixture:broad-research/otherlisted.txt",
    )
    assert resolved_nasdaq is not None and resolved_nasdaq.capture_id == nasdaq_capture.capture_id
    assert resolved_other is not None and resolved_other.capture_id == other_capture.capture_id
    assert resolved_nasdaq.capture_id != resolved_other.capture_id
