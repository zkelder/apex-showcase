from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apex.adapters.coinbase_public import FixtureCoinbaseCandleSource
from apex.application.crypto_campaign import (
    AttachCampaignDataset,
    AuthorizeHoldoutRelease,
    CaptureSealedHoldout,
    InMemoryCryptoCampaignRepository,
    MaterializeSealedHoldout,
    RecordCampaignExperiment,
    RecordDatasetExposure,
    RegisterCampaignCandidate,
    RegisterCryptoCampaign,
    SealHoldout,
    campaign_workspace,
    snapshot_access,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    CryptoResearchConflict,
    ExecuteCryptoTrendValidation,
    MaterializeCryptoHistoricalSnapshot,
    RegisterCryptoHypothesis,
)
from apex.domain.crypto_campaign import (
    EARLIER_EXPOSED_SNAPSHOT_ID,
    FIXED_CAMPAIGN_ID,
    FIXED_HOLDOUT_CAPTURE_ID,
    FIXED_HOLDOUT_ID,
    FIXED_HOLDOUT_SNAPSHOT_ID,
    LATER_EXPOSED_SNAPSHOT_ID,
    CampaignCandidate,
    CampaignDatasetRole,
    CampaignEvent,
    CampaignEventType,
    CampaignExperimentUse,
    CampaignLifecycle,
    DatasetExposureSummary,
    ExposureCategory,
    HoldoutState,
    SnapshotUsePurpose,
    holdout_state,
)
from apex.domain.research_evidence import CaptureStatus
from scripts.dev import parser

NOW = datetime(2026, 7, 29, 18, tzinfo=UTC)


def _fixture_snapshot(
    repository: InMemoryCryptoCampaignRepository,
    *,
    capture_id: str,
    snapshot_id: str,
    start: datetime,
) -> None:
    capture = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource()).start(
        capture_id=capture_id,
        start=start,
        end=start + timedelta(days=2),
        requested_at=NOW,
    )
    assert capture.status is CaptureStatus.COMPLETED
    MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id=snapshot_id,
        capture_id=capture_id,
        created_at=NOW + timedelta(seconds=1),
    )


def _campaign_repository() -> InMemoryCryptoCampaignRepository:
    repository = InMemoryCryptoCampaignRepository()
    _fixture_snapshot(
        repository,
        capture_id="earlier-development-capture-v1",
        snapshot_id=EARLIER_EXPOSED_SNAPSHOT_ID,
        start=datetime(2022, 7, 1, tzinfo=UTC),
    )
    _fixture_snapshot(
        repository,
        capture_id="later-development-capture-v1",
        snapshot_id=LATER_EXPOSED_SNAPSHOT_ID,
        start=datetime(2024, 7, 1, tzinfo=UTC),
    )
    for ordinal, snapshot_id in enumerate((EARLIER_EXPOSED_SNAPSHOT_ID, LATER_EXPOSED_SNAPSHOT_ID)):
        RecordDatasetExposure(repository).execute(
            exposure_id=f"development-exposure-{ordinal}",
            snapshot_id=snapshot_id,
            purpose="STRATEGY_DEVELOPMENT_REPORTING_COMPARISON_AND_DOSSIER",
            category=ExposureCategory.DEVELOPMENT_EXPOSED,
            first_known_exposure_at=NOW,
            recorded_at=NOW,
            actor="test",
            reason="Previously used by both rejected BTC strategies and operator reporting.",
            idempotency_key=f"exposure-key-{ordinal}",
            related_strategy_ids=("rejected-sma", "rejected-monthly"),
            related_experiment_ids=("earlier-run", "later-run"),
            related_dossier_ids=("sma-dossier", "monthly-dossier"),
        )
    campaign = RegisterCryptoCampaign(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=1),
        idempotency_key="campaign-register-key",
    )
    for ordinal, snapshot_id in enumerate(campaign.allowed_development_snapshot_ids):
        AttachCampaignDataset(repository).execute(
            attachment_id=f"development-attachment-{ordinal}",
            campaign_id=campaign.campaign_id,
            snapshot_id=snapshot_id,
            role=CampaignDatasetRole.DEVELOPMENT,
            actor="test",
            reason="Approved exposed development evidence.",
            attached_at=NOW + timedelta(minutes=2),
            idempotency_key=f"attach-key-{ordinal}",
        )
    return repository


def _candidate(ordinal: int) -> CampaignCandidate:
    return CampaignCandidate.create(
        candidate_id=f"candidate-{ordinal}",
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=f"hypothesis-{ordinal}",
        strategy_specification_id=f"strategy-{ordinal}",
        strategy_family=f"family-{ordinal}",
        strategy_semantics_hash=f"{ordinal + 1:064x}",
        rationale="Predeclared bounded long-or-cash candidate.",
        expected_mechanism="A distinct risk-management response to daily BTC evidence.",
        failure_conditions=("NEGATIVE_RETURN", "DRAWDOWN_NOT_IMPROVED"),
        exact_configuration_hash=f"{ordinal + 11:064x}",
        registered_at=NOW + timedelta(minutes=3, seconds=ordinal),
        actor="test",
    )


def test_exposure_is_append_only_and_never_restores_untouched_validation() -> None:
    repository = _campaign_repository()
    records = repository.exposures(EARLIER_EXPOSED_SNAPSHOT_ID)
    summary = DatasetExposureSummary.from_records(EARLIER_EXPOSED_SNAPSHOT_ID, records)
    assert summary.current_state == "EXPOSED_DEVELOPMENT_EVIDENCE"
    assert not summary.independent_validation_eligible
    assert not summary.untouched_validation_claim_permitted
    same = RecordDatasetExposure(repository).execute(
        exposure_id=records[0].exposure_id,
        snapshot_id=EARLIER_EXPOSED_SNAPSHOT_ID,
        purpose="ignored-on-idempotent-retry",
        category=ExposureCategory.SEALED_HOLDOUT,
        first_known_exposure_at=NOW,
        recorded_at=NOW,
        actor="test",
        reason="ignored",
        idempotency_key="retry-key",
    )
    assert same.canonical_hash == records[0].canonical_hash
    with pytest.raises(CryptoResearchConflict, match="CRYPTO_EXPOSURE_CANNOT_REVERSE_TO_SEALED"):
        RecordDatasetExposure(repository).execute(
            exposure_id="illegal-reseal",
            snapshot_id=EARLIER_EXPOSED_SNAPSHOT_ID,
            purpose="INVALID_RECLASSIFICATION",
            category=ExposureCategory.SEALED_HOLDOUT,
            first_known_exposure_at=NOW,
            recorded_at=NOW,
            actor="test",
            reason="Must fail.",
            idempotency_key="illegal-reseal-key",
        )


def test_campaign_budget_idempotence_replay_and_exhaustion() -> None:
    repository = _campaign_repository()
    candidates = []
    for ordinal in range(3):
        candidate = _candidate(ordinal)
        candidates.append(
            RegisterCampaignCandidate(repository).execute(
                candidate, idempotency_key=f"candidate-key-{ordinal}"
            )
        )
    assert (
        RegisterCampaignCandidate(repository).execute(
            candidates[0], idempotency_key="candidate-retry"
        )
        is candidates[0]
    )
    with pytest.raises(CryptoResearchConflict, match="CANDIDATE_BUDGET_EXHAUSTED"):
        RegisterCampaignCandidate(repository).execute(
            _candidate(3), idempotency_key="candidate-over-budget"
        )
    for ordinal in range(6):
        value = CampaignExperimentUse.create(
            consumption_id=f"consumption-{ordinal}",
            campaign_id=FIXED_CAMPAIGN_ID,
            candidate_id=candidates[ordinal % 3].candidate_id,
            run_id=f"run-{ordinal}",
            snapshot_id=(
                EARLIER_EXPOSED_SNAPSHOT_ID if ordinal % 2 == 0 else LATER_EXPOSED_SNAPSHOT_ID
            ),
            snapshot_exposure_state="EXPOSED_DEVELOPMENT_EVIDENCE",
            strategy_semantics_hash=candidates[ordinal % 3].strategy_semantics_hash,
            use_purpose=SnapshotUsePurpose.DEVELOPMENT,
            complete=True,
            reproducible=True,
            window_policy_passed=False,
            result_hash=f"{ordinal + 21:064x}",
            recorded_at=NOW + timedelta(minutes=4, seconds=ordinal),
            actor="test",
        )
        RecordCampaignExperiment(repository).execute(
            value, idempotency_key=f"experiment-key-{ordinal}"
        )
    replay = CampaignExperimentUse.create(
        consumption_id="replay-does-not-consume",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidates[0].candidate_id,
        run_id="run-0",
        snapshot_id=EARLIER_EXPOSED_SNAPSHOT_ID,
        snapshot_exposure_state="EXPOSED_DEVELOPMENT_EVIDENCE",
        strategy_semantics_hash=candidates[0].strategy_semantics_hash,
        use_purpose=SnapshotUsePurpose.DEVELOPMENT,
        complete=True,
        reproducible=True,
        window_policy_passed=False,
        result_hash=f"{21:064x}",
        recorded_at=NOW + timedelta(minutes=5),
        actor="test",
    )
    retained = RecordCampaignExperiment(repository).execute(replay, idempotency_key="replay-key")
    assert retained.consumption_id == "consumption-0"
    assert len(repository.campaign_experiments(FIXED_CAMPAIGN_ID)) == 6


def test_fixture_holdout_seals_and_all_ordinary_value_paths_refuse_access() -> None:
    repository = _campaign_repository()
    service = CaptureSealedHoldout(repository, FixtureCoinbaseCandleSource())
    capture = service.start(
        campaign_id=FIXED_CAMPAIGN_ID,
        capture_id=FIXED_HOLDOUT_CAPTURE_ID,
        actor="test",
        requested_at=NOW + timedelta(hours=1),
        idempotency_key="holdout-start",
    )
    replay = service.start(
        campaign_id=FIXED_CAMPAIGN_ID,
        capture_id=FIXED_HOLDOUT_CAPTURE_ID,
        actor="test",
        requested_at=NOW + timedelta(hours=1, seconds=1),
        idempotency_key="holdout-start",
    )
    assert replay == capture
    assert replay.request_count == 1
    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            campaign_id=FIXED_CAMPAIGN_ID,
            capture_id=FIXED_HOLDOUT_CAPTURE_ID,
            actor="test",
            requested_at=NOW + timedelta(hours=1, seconds=capture.request_count),
            idempotency_key=f"holdout-resume-{capture.request_count}",
        )
    assert capture.status is CaptureStatus.COMPLETED
    assert capture.expected_buckets == 730
    assert capture.accepted_buckets == 730
    assert capture.request_count == 3
    snapshot = MaterializeSealedHoldout(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        snapshot_id=FIXED_HOLDOUT_SNAPSHOT_ID,
        created_at=NOW + timedelta(hours=2),
        idempotency_key="holdout-materialize",
    )
    event = SealHoldout(repository).execute(
        event_id="holdout-sealed-event-v1",
        exposure_id="holdout-sealed-exposure-v1",
        attachment_id="holdout-attachment-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        snapshot_id=snapshot.snapshot_id,
        actor="test",
        reason="Seal exact fixture proof without claiming genuine evidence.",
        sealed_at=NOW + timedelta(hours=3),
        idempotency_key="holdout-seal",
    )
    assert event.state is HoldoutState.SEALED
    with pytest.raises(CryptoResearchConflict, match="VALUES_REDACTED"):
        repository.candles(FIXED_HOLDOUT_CAPTURE_ID)
    attempt = repository.attempts(FIXED_HOLDOUT_CAPTURE_ID)[0]
    with pytest.raises(CryptoResearchConflict, match="RAW_RESPONSE_REDACTED"):
        repository.raw_response(attempt.attempt_id)
    hypothesis, _ = RegisterCryptoHypothesis(repository).execute(
        hypothesis_id="sealed-executor-proof",
        revision=1,
        actor="test",
        created_at=NOW + timedelta(hours=4),
    )
    with pytest.raises(CryptoResearchConflict, match="VALUES_REDACTED"):
        ExecuteCryptoTrendValidation(repository).execute(
            run_id="sealed-holdout-run-must-not-exist",
            hypothesis_version_id=hypothesis.hypothesis_version_id,
            snapshot_id=FIXED_HOLDOUT_SNAPSHOT_ID,
            code_revision="0" * 40,
            executed_at=NOW + timedelta(hours=5),
        )
    assert repository.run("sealed-holdout-run-must-not-exist") is None
    decision = snapshot_access(
        repository,
        snapshot_id=FIXED_HOLDOUT_SNAPSHOT_ID,
        purpose=SnapshotUsePurpose.DEVELOPMENT,
        campaign_id=FIXED_CAMPAIGN_ID,
    )
    assert not decision.allowed
    assert decision.reason == "CRYPTO_SEALED_HOLDOUT_VALUES_REDACTED"
    workspace = campaign_workspace(repository)
    assert workspace["campaign_candidates"] == ()
    assert workspace["campaign_experiments"] == ()
    assert workspace["nominated_candidate"] is None
    assert all(
        getattr(item, "snapshot_id", None) != FIXED_HOLDOUT_SNAPSHOT_ID
        for item in workspace["snapshots"]  # type: ignore[union-attr]
    )


def test_release_requires_nomination_and_closed_development() -> None:
    repository = _campaign_repository()
    with pytest.raises(CryptoResearchConflict, match="NOMINATED_CANDIDATE_REQUIRED"):
        AuthorizeHoldoutRelease(repository).execute(
            campaign_event_id="release-campaign-event",
            holdout_event_id="release-holdout-event",
            exposure_id="release-exposure",
            campaign_id=FIXED_CAMPAIGN_ID,
            candidate_id="missing-candidate",
            actor="test",
            reason="Must fail.",
            authorized_at=NOW,
            idempotency_key="release-key",
        )
    candidate = RegisterCampaignCandidate(repository).execute(
        _candidate(0), idempotency_key="candidate-key"
    )
    campaign = repository.campaign(FIXED_CAMPAIGN_ID)
    assert campaign is not None
    repository.save_campaign_event(
        CampaignEvent.create(
            event_id="candidate-nominated",
            campaign_id=FIXED_CAMPAIGN_ID,
            event_type=CampaignEventType.CANDIDATE_NOMINATED,
            candidate_id=candidate.candidate_id,
            prior_state=CampaignLifecycle.DEVELOPMENT_ACTIVE,
            actor="test",
            reason="Deterministic transition proof.",
            occurred_at=NOW + timedelta(hours=1),
        )
    )
    repository.save_campaign_event(
        CampaignEvent.create(
            event_id="development-closed",
            campaign_id=FIXED_CAMPAIGN_ID,
            event_type=CampaignEventType.DEVELOPMENT_CLOSED,
            candidate_id=candidate.candidate_id,
            prior_state=CampaignLifecycle.CANDIDATE_NOMINATED,
            actor="test",
            reason="Deterministic transition proof.",
            occurred_at=NOW + timedelta(hours=2),
        )
    )
    assert holdout_state(repository.holdout_events(FIXED_HOLDOUT_ID)) is None
    with pytest.raises(CryptoResearchConflict, match="HOLDOUT_NOT_SEALED"):
        AuthorizeHoldoutRelease(repository).execute(
            campaign_event_id="release-campaign-event",
            holdout_event_id="release-holdout-event",
            exposure_id="release-exposure",
            campaign_id=FIXED_CAMPAIGN_ID,
            candidate_id=candidate.candidate_id,
            actor="test",
            reason="Must fail until exact holdout is sealed.",
            authorized_at=NOW + timedelta(hours=3),
            idempotency_key="release-key-2",
        )


def test_exact_id_cli_surface_requires_ids_and_idempotency_keys() -> None:
    parsed = parser().parse_args(
        [
            "crypto-campaign-register",
            "--campaign-id",
            FIXED_CAMPAIGN_ID,
            "--actor",
            "test",
            "--idempotency-key",
            "register-key",
        ]
    )
    assert parsed.campaign_id == FIXED_CAMPAIGN_ID
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "crypto-holdout-seal",
                "--campaign-id",
                FIXED_CAMPAIGN_ID,
                "--snapshot-id",
                FIXED_HOLDOUT_SNAPSHOT_ID,
            ]
        )
