from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.coinbase_public import FixtureCoinbaseCandleSource
from apex.application.crypto_campaign import (
    AttachCampaignDataset,
    CaptureSealedHoldout,
    MaterializeSealedHoldout,
    RecordDatasetExposure,
    RegisterCryptoCampaign,
    SealHoldout,
)
from apex.application.crypto_campaign_candidate import (
    ApplyCandidateNominationPolicy,
    ExecuteCandidateWindow,
    InMemoryCryptoCampaignCandidateRepository,
    MaterializeCandidateDossier,
    RecordCandidateOutcome,
    RegisterCandidate,
    RegisterCandidateHypothesis,
    RegisterCandidateProtocol,
    RegisterCandidateStrategy,
    candidate_workspace,
)
from apex.application.crypto_campaign_candidate_2 import (
    ApplyBreakoutNominationPolicy,
    ExecuteBreakoutWindow,
    InMemoryCryptoCampaignCandidate2Repository,
    MaterializeBreakoutDossier,
    RecordBreakoutOutcome,
    RegisterBreakoutCandidate,
    RegisterBreakoutHypothesis,
    RegisterBreakoutProtocol,
    RegisterBreakoutStrategy,
    candidate_2_workspace,
)
from apex.application.crypto_challenger import (
    ExecuteCryptoChallengerWindow,
    InMemoryCryptoChallengerRepository,
    MaterializeCryptoChallengerDossier,
    RecordCryptoChallengerConclusion,
    RecordCryptoChallengerOutcome,
    RegisterCryptoChallengerProtocol,
    RegisterMonthlyMomentumHypothesis,
    RegisterMonthlyMomentumStrategy,
    crypto_challenger_workspace,
)
from apex.application.crypto_dossier import (
    ExecuteCryptoDisjointReplication,
    RegisterCryptoReplicationProtocol,
    RegisterFrozenCryptoStrategy,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    CryptoResearchConflict,
    ExecuteCryptoTrendValidation,
    MaterializeCryptoHistoricalSnapshot,
    RecordCryptoExperimentOutcome,
    RegisterCryptoHypothesis,
)
from apex.domain.crypto_campaign import (
    FIXED_CAMPAIGN_ID,
    FIXED_HOLDOUT_CAPTURE_ID,
    FIXED_HOLDOUT_SNAPSHOT_ID,
    CampaignDatasetRole,
    ExposureCategory,
)
from apex.domain.crypto_campaign_candidate import (
    CandidatePortfolio,
    build_monthly_200_decisions,
    calculate_candidate_metrics,
    candidate_metric,
)
from apex.domain.crypto_campaign_candidate_2 import (
    BreakoutPortfolio,
    BreakoutPositionState,
    BreakoutRule,
    BreakoutTransitionReason,
    breakout_metric,
    build_monthly_stateful_breakout_decisions,
    calculate_breakout_metrics,
)
from apex.domain.crypto_challenger import (
    CHANGED_SEMANTICS,
    EARLIER_PRIOR_RUN_ID,
    EARLIER_SNAPSHOT_ID,
    EARLIER_VALIDATION_END,
    EARLIER_VALIDATION_START,
    LATER_SNAPSHOT_ID,
    LATER_VALIDATION_END,
    LATER_VALIDATION_START,
    NO_LEADING_CHALLENGER,
    ChallengerConclusionType,
    ChallengerPortfolio,
    CryptoChallengerError,
    StrategySemanticComparison,
    build_monthly_momentum_decisions,
    calculate_challenger_metrics,
    conservative_challenger_metric,
)
from apex.domain.crypto_dossier import (
    NO_APPROVED_CHAMPION,
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
from apex.domain.crypto_research import (
    FIXED_DATA_END,
    FIXED_DATA_START,
    CostScenarioName,
    CryptoDailyCandle,
)
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
)
from apex.operator_api import app
from scripts.dev import parser

NOW = datetime(2026, 7, 29, 20, tzinfo=UTC)
REVISION = "c" * 40
PARENT_ID = "btc-usd-daily-sma-20-100-rejected-v1"


def _complete_capture(
    repository: InMemoryCryptoChallengerRepository,
    *,
    capture_id: str,
    start: datetime,
    end: datetime,
):
    service = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource())
    capture = service.start(capture_id=capture_id, start=start, end=end, requested_at=NOW)
    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            capture_id=capture_id,
            requested_at=NOW + timedelta(seconds=capture.request_count),
        )
    return capture


def _repository_with_retained_evidence(
    repository: InMemoryCryptoChallengerRepository | None = None,
) -> InMemoryCryptoChallengerRepository:
    repository = repository or InMemoryCryptoChallengerRepository()
    hypothesis, specification = RegisterCryptoHypothesis(repository).execute(
        hypothesis_id="fixture-original",
        revision=1,
        actor="test",
        created_at=NOW,
    )
    later_capture = _complete_capture(
        repository,
        capture_id="fixture-later-capture",
        start=FIXED_DATA_START,
        end=FIXED_DATA_END,
    )
    later_snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="fixture-later-snapshot",
        capture_id=later_capture.capture_id,
        created_at=NOW,
    )
    later_run = ExecuteCryptoTrendValidation(repository).execute(
        run_id="fixture-later-run",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=later_snapshot.snapshot_id,
        code_revision="a" * 40,
        executed_at=NOW,
    )
    later_outcome = RecordCryptoExperimentOutcome(repository).execute(
        outcome_id="fixture-later-outcome",
        run_id=later_run.run_id,
        actor="test",
        decided_at=NOW,
    )
    repository._hypotheses[ORIGINAL_HYPOTHESIS_VERSION_ID] = replace(
        hypothesis, hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID
    )
    repository._specifications[ORIGINAL_HYPOTHESIS_VERSION_ID] = replace(
        specification, hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID
    )
    repository._snapshots[ORIGINAL_SNAPSHOT_ID] = replace(
        later_snapshot,
        snapshot_id=ORIGINAL_SNAPSHOT_ID,
        manifest_hash=ORIGINAL_SNAPSHOT_MANIFEST_HASH,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    repository._runs[ORIGINAL_RUN_ID] = replace(
        later_run,
        run_id=ORIGINAL_RUN_ID,
        hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
        snapshot_id=ORIGINAL_SNAPSHOT_ID,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        result_hash=ORIGINAL_RESULT_HASH,
        canonical_hash=ORIGINAL_CANONICAL_RUN_HASH,
    )
    repository._outcomes[ORIGINAL_OUTCOME_ID] = replace(
        later_outcome,
        outcome_id=ORIGINAL_OUTCOME_ID,
        run_id=ORIGINAL_RUN_ID,
        decision=ExperimentOutcomeDecision.REJECTED,
        paper_eligible=False,
        live_eligible=False,
        canonical_hash=ORIGINAL_OUTCOME_HASH,
    )
    parent = RegisterFrozenCryptoStrategy(repository).execute(
        specification_id=PARENT_ID, created_at=NOW
    )
    earlier_capture = _complete_capture(
        repository,
        capture_id="fixture-earlier-capture",
        start=REPLICATION_DATA_START,
        end=REPLICATION_DATA_END,
    )
    earlier_snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="fixture-earlier-snapshot",
        capture_id=earlier_capture.capture_id,
        created_at=NOW,
    )
    exact_earlier = replace(
        earlier_snapshot,
        snapshot_id=EARLIER_SNAPSHOT_ID,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    repository._snapshots[EARLIER_SNAPSHOT_ID] = exact_earlier
    protocol = RegisterCryptoReplicationProtocol(repository).execute(
        protocol_id="fixture-prior-replication-protocol",
        strategy_specification_id=parent.specification_id,
        created_at=NOW,
    )
    earlier_run = ExecuteCryptoDisjointReplication(repository).execute(
        run_id="fixture-prior-replication-run",
        protocol_id=protocol.protocol_id,
        snapshot_id=EARLIER_SNAPSHOT_ID,
        code_revision="b" * 40,
        executed_at=NOW,
    )
    repository._replication_runs[EARLIER_PRIOR_RUN_ID] = replace(
        earlier_run, run_id=EARLIER_PRIOR_RUN_ID
    )
    return repository


def _register_governance(repository: InMemoryCryptoChallengerRepository):
    hypothesis, hypothesis_specification = RegisterMonthlyMomentumHypothesis(repository).execute(
        hypothesis_id="btc-monthly-momentum-challenger",
        actor="test",
        created_at=NOW + timedelta(minutes=1),
    )
    strategy, comparison = RegisterMonthlyMomentumStrategy(repository).execute(
        specification_id="btc-monthly-momentum-252-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        parent_specification_id=PARENT_ID,
        created_at=NOW + timedelta(minutes=2),
    )
    protocol = RegisterCryptoChallengerProtocol(repository).execute(
        protocol_id="btc-monthly-momentum-two-window-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        code_revision=REVISION,
        created_at=NOW + timedelta(minutes=3),
    )
    return hypothesis, hypothesis_specification, strategy, comparison, protocol


def _daily_candles(start: datetime, count: int) -> tuple[CryptoDailyCandle, ...]:
    result = []
    for ordinal in range(count):
        price = Decimal(100) + Decimal(ordinal)
        result.append(
            CryptoDailyCandle.create(
                capture_id="unit-capture",
                attempt_id="unit-attempt",
                evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
                bucket_start=start + timedelta(days=ordinal),
                open=price,
                high=price + 2,
                low=price - 2,
                close=price + 1,
                volume="10",
                ingested_at=NOW,
                raw_response_hash="d" * 64,
            )
        )
    return tuple(result)


def test_monthly_252_signal_is_calendar_bounded_prior_only_and_next_open() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    decisions = build_monthly_momentum_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    assert len(decisions) == 12
    assert all(value.eligible and value.desired_long for value in decisions)
    assert all(value.execution_at == value.decision_month for value in decisions)
    assert all(
        value.information_available_at is not None
        and value.execution_at is not None
        and value.information_available_at <= value.execution_at
        for value in decisions
    )
    assert all(
        value.information_through is not None
        and value.trailing_bucket_start is not None
        and (value.information_through - value.trailing_bucket_start).days == 252
        for value in decisions
    )
    leap = next(value for value in decisions if value.decision_month.month == 3)
    assert leap.information_through == datetime(2024, 2, 29, tzinfo=UTC)


def test_missing_month_end_or_trailing_observation_fails_closed_without_bridge() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    without_month_end = tuple(
        value for value in candles if value.bucket_start != datetime(2023, 6, 30, tzinfo=UTC)
    )
    first = build_monthly_momentum_decisions(
        without_month_end,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert not first.eligible
    assert first.failure_reason == "FINAL_PRECEDING_MONTH_OBSERVATION_MISSING"
    without_trailing = tuple(
        value for value in candles if value.bucket_start != datetime(2022, 10, 21, tzinfo=UTC)
    )
    first = build_monthly_momentum_decisions(
        without_trailing,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert not first.eligible
    assert first.failure_reason == "TRAILING_252_OBSERVATION_GAP"


def test_missing_first_calendar_day_uses_next_open_without_same_close_leakage() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    candles = tuple(
        value for value in candles if value.bucket_start != datetime(2023, 7, 1, tzinfo=UTC)
    )
    first = build_monthly_momentum_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert first.eligible
    assert first.execution_at == datetime(2023, 7, 2, tzinfo=UTC)
    assert first.information_through == datetime(2023, 6, 30, tzinfo=UTC)
    changed_execution_close = tuple(
        replace(value, close=Decimal("999999"))
        if value.bucket_start == first.execution_at
        else value
        for value in candles
    )
    replay = build_monthly_momentum_decisions(
        changed_execution_close,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert replay.momentum == first.momentum
    assert replay.desired_long == first.desired_long


def test_cost_scenarios_retain_fee_slippage_and_required_metrics() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    decisions = build_monthly_momentum_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    metrics = calculate_challenger_metrics(
        candles=candles,
        decisions=decisions,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    zero = next(
        value
        for value in metrics
        if value.portfolio is ChallengerPortfolio.MONTHLY_MOMENTUM
        and value.scenario is CostScenarioName.DIAGNOSTIC_ZERO_COST
    )
    conservative = conservative_challenger_metric(metrics, ChallengerPortfolio.MONTHLY_MOMENTUM)
    assert zero.modeled_fees == zero.modeled_slippage == zero.total_modeled_cost == 0
    assert conservative.modeled_fees > 0
    assert conservative.modeled_slippage > 0
    assert (
        conservative.total_modeled_cost == conservative.modeled_fees + conservative.modeled_slippage
    )
    assert conservative.days_long + conservative.days_in_cash == 366
    assert conservative.rebalance_decisions == 12
    assert conservative.failed_or_skipped_rebalances == 0
    assert conservative.completed_round_trips >= 1


def test_material_change_lineage_duplicate_detection_and_hypothesis_immutability() -> None:
    repository = _repository_with_retained_evidence()
    hypothesis, _, strategy, comparison, _ = _register_governance(repository)
    assert tuple(value[0] for value in comparison.changed) == CHANGED_SEMANTICS
    assert comparison.material_change
    assert strategy.parent_specification_id == PARENT_ID
    assert strategy.version == 2
    same_hypothesis, _ = RegisterMonthlyMomentumHypothesis(repository).execute(
        hypothesis_id=hypothesis.hypothesis_id,
        actor="different-actor-is-ignored-on-idempotent-replay",
        created_at=NOW + timedelta(days=1),
    )
    assert same_hypothesis.canonical_hash == hypothesis.canonical_hash
    duplicate = replace(strategy, specification_id="renamed-only")
    with pytest.raises(CryptoResearchConflict, match="DUPLICATE_STRATEGY"):
        repository.save_strategy_specification(duplicate)
    with pytest.raises(CryptoChallengerError, match="MATERIAL_CHANGE_REQUIRED"):
        StrategySemanticComparison.create(
            prior=repository.strategy_specification(PARENT_ID),
            challenger=replace(
                strategy,
                strategy_family=repository.strategy_specification(PARENT_ID).strategy_family,
                semantics=repository.strategy_specification(PARENT_ID).semantics,
                semantics_hash=repository.strategy_specification(PARENT_ID).semantics_hash,
            ),
        )


def test_two_retained_windows_use_one_protocol_replay_and_preserve_prior_hashes() -> None:
    repository = _repository_with_retained_evidence()
    _, _, strategy, _, protocol = _register_governance(repository)
    earlier = ExecuteCryptoChallengerWindow(repository).execute(
        run_id="challenger-earlier-v1",
        protocol_id=protocol.protocol_id,
        snapshot_id=EARLIER_SNAPSHOT_ID,
        code_revision=REVISION,
        executed_at=NOW + timedelta(minutes=4),
    )
    later = ExecuteCryptoChallengerWindow(repository).execute(
        run_id="challenger-later-v1",
        protocol_id=protocol.protocol_id,
        snapshot_id=LATER_SNAPSHOT_ID,
        code_revision=REVISION,
        executed_at=NOW + timedelta(minutes=5),
    )
    assert earlier.strategy_semantics_hash == later.strategy_semantics_hash
    assert earlier.strategy_semantics_hash == strategy.semantics_hash
    assert (earlier.validation_start, earlier.validation_end) == (
        EARLIER_VALIDATION_START,
        EARLIER_VALIDATION_END,
    )
    assert (later.validation_start, later.validation_end) == (
        LATER_VALIDATION_START,
        LATER_VALIDATION_END,
    )
    assert ExecuteCryptoChallengerWindow(repository).replay(earlier.run_id) is earlier
    assert ExecuteCryptoChallengerWindow(repository).replay(later.run_id) is later
    assert repository.run(ORIGINAL_RUN_ID).result_hash == ORIGINAL_RESULT_HASH
    assert repository.run(ORIGINAL_RUN_ID).canonical_hash == ORIGINAL_CANONICAL_RUN_HASH
    assert earlier.prior_comparison.prior_run_id == EARLIER_PRIOR_RUN_ID
    assert later.prior_comparison.prior_run_id == ORIGINAL_RUN_ID


def test_dossier_conclusion_workspace_and_zero_authority() -> None:
    repository = _repository_with_retained_evidence()
    _, _, _, _, protocol = _register_governance(repository)
    runs = tuple(
        ExecuteCryptoChallengerWindow(repository).execute(
            run_id=f"challenger-{index}-v1",
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            executed_at=NOW + timedelta(minutes=4 + index),
        )
        for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID))
    )
    outcomes = tuple(
        RecordCryptoChallengerOutcome(repository).execute(
            outcome_id=f"challenger-outcome-{index}-v1",
            run_id=run.run_id,
            actor="test",
            decided_at=NOW + timedelta(minutes=10 + index),
        )
        for index, run in enumerate(runs)
    )
    dossier = MaterializeCryptoChallengerDossier(repository).execute(
        dossier_id="challenger-dossier-v1",
        protocol_id=protocol.protocol_id,
        earlier_outcome_id=outcomes[0].outcome_id,
        later_outcome_id=outcomes[1].outcome_id,
        created_at=NOW + timedelta(minutes=20),
    )
    conclusion = RecordCryptoChallengerConclusion(repository).execute(
        conclusion_id="challenger-conclusion-v1",
        dossier_id=dossier.dossier_id,
        actor="test",
        decided_at=NOW + timedelta(minutes=21),
    )
    assert len(str(dossier.median_conservative_return).split(".")[-1]) <= 8
    assert len(str(dossier.compounded_conservative_return).split(".")[-1]) <= 8
    assert dossier.passing_windows + dossier.failing_windows + dossier.inconclusive_windows == 2
    assert conclusion.conclusion in ChallengerConclusionType
    workspace = crypto_challenger_workspace(repository)
    assert workspace["approved_champion_status"] == NO_APPROVED_CHAMPION
    assert workspace["leading_challenger_status"] in {
        NO_LEADING_CHALLENGER,
        "LEADING_RESEARCH_CHALLENGER_REQUIRES_FORWARD_VALIDATION",
    }
    assert not workspace["paper_eligible"] and not workspace["live_eligible"]
    assert workspace["capital_authority"] == workspace["order_authority"] == "NONE"
    assert workspace["financial_mutations_possible"] is False


def test_exact_id_cli_and_read_only_api_surface() -> None:
    for command, arguments in (
        (
            "crypto-challenger-hypothesis-register",
            ("--hypothesis-id", "h", "--actor", "a"),
        ),
        (
            "crypto-challenger-strategy-register",
            (
                "--specification-id",
                "s",
                "--hypothesis-version-id",
                "h",
                "--parent-specification-id",
                "p",
            ),
        ),
        (
            "crypto-challenger-window-run",
            ("--run-id", "r", "--protocol-id", "p", "--snapshot-id", "s"),
        ),
        (
            "crypto-challenger-dossier-materialize",
            (
                "--dossier-id",
                "d",
                "--protocol-id",
                "p",
                "--earlier-outcome-id",
                "e",
                "--later-outcome-id",
                "l",
            ),
        ),
        (
            "crypto-candidate-2-hypothesis-register",
            (
                "--hypothesis-id",
                "h2",
                "--campaign-id",
                "c",
                "--actor",
                "a",
                "--idempotency-key",
                "k",
            ),
        ),
        (
            "crypto-candidate-2-window-run",
            (
                "--run-id",
                "r2",
                "--consumption-id",
                "x2",
                "--protocol-id",
                "p2",
                "--snapshot-id",
                "s2",
                "--actor",
                "a",
                "--idempotency-key",
                "k",
            ),
        ),
        (
            "crypto-candidate-2-nomination-apply",
            (
                "--evaluation-id",
                "e2",
                "--dossier-id",
                "d2",
                "--nomination-event-id",
                "n2",
                "--development-closed-event-id",
                "z2",
                "--actor",
                "a",
                "--reason",
                "r",
                "--idempotency-key",
                "k",
            ),
        ),
    ):
        assert parser().parse_args((command, *arguments)).command == command
    methods = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", set())
    }
    assert ("/api/v1/crypto-research/challenger-runs/{run_id}", "GET") in methods
    assert (
        "/api/v1/crypto-research/challenger-dossiers/{dossier_id}",
        "GET",
    ) in methods
    assert ("/api/v1/crypto-research/approved-champion", "GET") in methods


def _campaign_candidate_repository() -> InMemoryCryptoCampaignCandidateRepository:
    repository = InMemoryCryptoCampaignCandidate2Repository()
    _repository_with_retained_evidence(repository)
    _, _, _, _, prior_protocol = _register_governance(repository)
    for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID)):
        ExecuteCryptoChallengerWindow(repository).execute(
            run_id=f"retained-monthly-252-{index}",
            protocol_id=prior_protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            executed_at=NOW + timedelta(minutes=10 + index),
        )
        RecordDatasetExposure(repository).execute(
            exposure_id=f"candidate-development-exposure-{index}",
            snapshot_id=snapshot_id,
            purpose="CAMPAIGN_CANDIDATE_DEVELOPMENT",
            category=ExposureCategory.DEVELOPMENT_EXPOSED,
            first_known_exposure_at=NOW,
            recorded_at=NOW + timedelta(minutes=20 + index),
            actor="test",
            reason="Already exposed development evidence.",
            idempotency_key=f"candidate-development-exposure-key-{index}",
            related_strategy_ids=("rejected-sma", "rejected-monthly-252"),
        )
    campaign = RegisterCryptoCampaign(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=25),
        idempotency_key="candidate-campaign-register",
    )
    for index, snapshot_id in enumerate(campaign.allowed_development_snapshot_ids):
        AttachCampaignDataset(repository).execute(
            attachment_id=f"candidate-development-attachment-{index}",
            campaign_id=campaign.campaign_id,
            snapshot_id=snapshot_id,
            role=CampaignDatasetRole.DEVELOPMENT,
            actor="test",
            reason="Approved exposed development evidence.",
            attached_at=NOW + timedelta(minutes=26),
            idempotency_key=f"candidate-development-attachment-key-{index}",
        )
    holdout_service = CaptureSealedHoldout(repository, FixtureCoinbaseCandleSource())
    holdout_capture = holdout_service.start(
        campaign_id=campaign.campaign_id,
        capture_id=FIXED_HOLDOUT_CAPTURE_ID,
        actor="test",
        requested_at=NOW + timedelta(minutes=27),
        idempotency_key="candidate-holdout-start",
    )
    while holdout_capture.status is CaptureStatus.RUNNING:
        holdout_capture = holdout_service.resume(
            campaign_id=campaign.campaign_id,
            capture_id=FIXED_HOLDOUT_CAPTURE_ID,
            actor="test",
            requested_at=NOW
            + timedelta(
                minutes=27,
                seconds=holdout_capture.request_count,
            ),
            idempotency_key=(f"candidate-holdout-resume-{holdout_capture.request_count}"),
        )
    holdout_snapshot = MaterializeSealedHoldout(repository).execute(
        campaign_id=campaign.campaign_id,
        snapshot_id=FIXED_HOLDOUT_SNAPSHOT_ID,
        created_at=NOW + timedelta(minutes=28),
        idempotency_key="candidate-holdout-materialize",
    )
    SealHoldout(repository).execute(
        event_id="candidate-holdout-sealed",
        exposure_id="candidate-holdout-sealed-exposure",
        attachment_id="candidate-holdout-attachment",
        campaign_id=campaign.campaign_id,
        snapshot_id=holdout_snapshot.snapshot_id,
        actor="test",
        reason="Seal fixture holdout for candidate governance tests.",
        sealed_at=NOW + timedelta(minutes=29),
        idempotency_key="candidate-holdout-seal",
    )
    return repository


def test_monthly_200_mean_includes_current_month_end_and_has_no_same_candle_leakage() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    decision = build_monthly_200_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    required = tuple(
        item
        for item in candles
        if datetime(2022, 12, 13, tzinfo=UTC)
        <= item.bucket_start
        <= datetime(2023, 6, 30, tzinfo=UTC)
    )
    assert len(required) == decision.observation_count == 200
    assert decision.arithmetic_mean == (
        sum((item.close for item in required), Decimal(0)) / Decimal(200)
    ).quantize(Decimal("0.00000001"))
    assert decision.information_through == datetime(2023, 6, 30, tzinfo=UTC)
    assert decision.execution_at == datetime(2023, 7, 1, tzinfo=UTC)
    assert decision.information_through < decision.execution_at
    changed = tuple(
        replace(item, close=Decimal("999999"))
        if item.bucket_start == decision.execution_at
        else item
        for item in candles
    )
    replay = build_monthly_200_decisions(
        changed,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert replay.arithmetic_mean == decision.arithmetic_mean
    assert replay.desired_long == decision.desired_long


def test_monthly_200_fails_closed_for_gaps_and_preserves_leap_day() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    missing = tuple(
        item for item in candles if item.bucket_start != datetime(2023, 1, 1, tzinfo=UTC)
    )
    first = build_monthly_200_decisions(
        missing,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert not first.eligible
    assert first.ineligibility_reason == "REQUIRED_200_OBSERVATION_SEQUENCE_HAS_GAP"
    decisions = build_monthly_200_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    march = next(item for item in decisions if item.decision_month.month == 3)
    assert march.information_through == datetime(2024, 2, 29, tzinfo=UTC)
    assert all(item.observation_count == 200 for item in decisions)


def test_monthly_200_costs_are_separate_and_metrics_are_deterministic() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    decisions = build_monthly_200_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    metrics = calculate_candidate_metrics(
        candles=candles,
        decisions=decisions,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    zero = candidate_metric(
        metrics,
        CandidatePortfolio.MONTHLY_200_PRICE_TREND,
        CostScenarioName.DIAGNOSTIC_ZERO_COST,
    )
    conservative = candidate_metric(metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND)
    assert zero.modeled_fees == zero.modeled_slippage == zero.total_modeled_cost == 0
    assert conservative.modeled_fees > 0
    assert conservative.modeled_slippage > 0
    assert conservative.total_modeled_cost == (
        conservative.modeled_fees + conservative.modeled_slippage
    )
    assert conservative.monthly_decisions == conservative.eligible_decisions == 12
    assert conservative.ineligible_decisions == 0
    assert len(conservative.holdings) == 366
    replay = calculate_candidate_metrics(
        candles=candles,
        decisions=decisions,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    assert tuple(item.canonical_hash for item in replay) == tuple(
        item.canonical_hash for item in metrics
    )


def test_monthly_stateful_90_45_is_strict_prior_only_and_persists_state() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    decisions = build_monthly_stateful_breakout_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 9, 1, tzinfo=UTC),
    )
    first, second = decisions
    assert first.prior_state is BreakoutPositionState.CASH
    assert first.evaluated_rule is BreakoutRule.ENTRY_90_CLOSE_MAXIMUM
    assert first.reference_count == 90
    assert first.reference_end == datetime(2023, 6, 29, tzinfo=UTC)
    assert first.information_through == datetime(2023, 6, 30, tzinfo=UTC)
    assert first.execution_at == datetime(2023, 7, 1, tzinfo=UTC)
    assert first.trigger
    assert first.resulting_state is BreakoutPositionState.LONG
    assert second.prior_state is BreakoutPositionState.LONG
    assert second.evaluated_rule is BreakoutRule.EXIT_45_CLOSE_MINIMUM
    assert not second.trigger
    assert second.resulting_state is BreakoutPositionState.LONG
    changed_execution_close = tuple(
        replace(item, close=Decimal("1")) if item.bucket_start == first.execution_at else item
        for item in candles
    )
    replay = build_monthly_stateful_breakout_decisions(
        changed_execution_close,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert replay.canonical_hash == first.canonical_hash


def test_monthly_stateful_90_45_equality_and_invalid_state_rules_fail_closed() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    june_29 = next(
        item for item in candles if item.bucket_start == datetime(2023, 6, 29, tzinfo=UTC)
    )
    equal = tuple(
        replace(item, close=june_29.close)
        if item.bucket_start == datetime(2023, 6, 30, tzinfo=UTC)
        else item
        for item in candles
    )
    equal_decision = build_monthly_stateful_breakout_decisions(
        equal,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert equal_decision.eligible
    assert not equal_decision.trigger
    assert equal_decision.resulting_state is BreakoutPositionState.CASH
    missing_warmup = tuple(
        item for item in candles if item.bucket_start != datetime(2023, 6, 1, tzinfo=UTC)
    )
    cash_decision = build_monthly_stateful_breakout_decisions(
        missing_warmup,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 8, 1, tzinfo=UTC),
    )[0]
    assert not cash_decision.eligible
    assert cash_decision.resulting_state is BreakoutPositionState.CASH
    assert cash_decision.transition_reason is BreakoutTransitionReason.INELIGIBLE_REMAIN_CASH
    missing_long_reference = tuple(
        item for item in candles if item.bucket_start != datetime(2023, 7, 15, tzinfo=UTC)
    )
    long_decisions = build_monthly_stateful_breakout_decisions(
        missing_long_reference,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=datetime(2023, 9, 1, tzinfo=UTC),
    )
    assert long_decisions[0].resulting_state is BreakoutPositionState.LONG
    assert not long_decisions[1].eligible
    assert (
        long_decisions[1].transition_reason is BreakoutTransitionReason.PROTECTIVE_DATA_QUALITY_EXIT
    )
    assert long_decisions[1].resulting_state is BreakoutPositionState.CASH


def test_monthly_stateful_90_45_costs_and_metrics_are_deterministic() -> None:
    candles = _daily_candles(datetime(2022, 7, 1, tzinfo=UTC), 731)
    decisions = build_monthly_stateful_breakout_decisions(
        candles,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    metrics = calculate_breakout_metrics(
        candles=candles,
        decisions=decisions,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    zero = breakout_metric(
        metrics,
        BreakoutPortfolio.MONTHLY_STATEFUL_BREAKOUT,
        CostScenarioName.DIAGNOSTIC_ZERO_COST,
    )
    conservative = breakout_metric(metrics, BreakoutPortfolio.MONTHLY_STATEFUL_BREAKOUT)
    assert zero.modeled_fees == zero.modeled_slippage == 0
    assert conservative.modeled_fees > 0
    assert conservative.modeled_slippage > 0
    assert conservative.monthly_decisions == conservative.eligible_decisions == 12
    assert conservative.entry_rule_evaluations > 0
    assert conservative.exit_rule_evaluations > 0
    replay = calculate_breakout_metrics(
        candles=candles,
        decisions=decisions,
        validation_start=EARLIER_VALIDATION_START,
        validation_end=EARLIER_VALIDATION_END,
    )
    assert tuple(item.canonical_hash for item in replay) == tuple(
        item.canonical_hash for item in metrics
    )


def test_campaign_candidate_full_two_window_budget_dossier_and_policy() -> None:
    repository = _campaign_candidate_repository()
    hypothesis, _ = RegisterCandidateHypothesis(repository).execute(
        hypothesis_id="btc-monthly-200-candidate-hypothesis",
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=30),
        idempotency_key="candidate-hypothesis-register",
    )
    strategy, comparisons = RegisterCandidateStrategy(repository).execute(
        specification_id="btc-monthly-200-price-trend-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        created_at=NOW + timedelta(minutes=31),
        idempotency_key="candidate-strategy-register",
    )
    candidate = RegisterCandidate(repository).execute(
        candidate_id="btc-monthly-200-candidate-1",
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(minutes=32),
        idempotency_key="candidate-register",
    )
    same = RegisterCandidate(repository).execute(
        candidate_id=candidate.candidate_id,
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(hours=1),
        idempotency_key="candidate-register-replay",
    )
    assert same.canonical_hash == candidate.canonical_hash
    assert comparisons.against_sma_20_100.materially_distinct
    assert comparisons.against_monthly_252.materially_distinct
    assert len(repository.campaign_candidates(FIXED_CAMPAIGN_ID)) == 1
    protocol = RegisterCandidateProtocol(repository).execute(
        protocol_id="btc-monthly-200-two-window-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        code_revision=REVISION,
        created_at=NOW + timedelta(minutes=33),
        idempotency_key="candidate-protocol-register",
    )
    assert (
        RegisterCandidateProtocol(repository).execute(
            protocol_id=protocol.protocol_id,
            campaign_id=FIXED_CAMPAIGN_ID,
            candidate_id=candidate.candidate_id,
            code_revision=REVISION,
            created_at=NOW + timedelta(hours=1),
            idempotency_key="candidate-protocol-register-replay",
        )
        is protocol
    )
    runs = tuple(
        ExecuteCandidateWindow(repository).execute(
            run_id=f"btc-monthly-200-window-{index}",
            consumption_id=f"btc-monthly-200-consumption-{index}",
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            actor="test",
            executed_at=NOW + timedelta(minutes=34 + index),
            idempotency_key=f"candidate-window-{index}",
        )
        for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID))
    )
    before_replay = len(repository.campaign_experiments(FIXED_CAMPAIGN_ID))
    assert (
        ExecuteCandidateWindow(repository).execute(
            run_id=runs[0].run_id,
            consumption_id="btc-monthly-200-consumption-0",
            protocol_id=protocol.protocol_id,
            snapshot_id=EARLIER_SNAPSHOT_ID,
            code_revision=REVISION,
            actor="test",
            executed_at=NOW + timedelta(hours=1),
            idempotency_key="candidate-window-0-retry",
        )
        is runs[0]
    )
    assert ExecuteCandidateWindow(repository).replay(runs[0].run_id) is runs[0]
    assert len(repository.campaign_experiments(FIXED_CAMPAIGN_ID)) == before_replay == 2
    outcomes = tuple(
        RecordCandidateOutcome(repository).execute(
            outcome_id=f"btc-monthly-200-outcome-{index}",
            run_id=run.run_id,
            actor="test",
            decided_at=NOW + timedelta(minutes=40 + index),
            idempotency_key=f"candidate-outcome-{index}",
        )
        for index, run in enumerate(runs)
    )
    dossier = MaterializeCandidateDossier(repository).execute(
        dossier_id="btc-monthly-200-dossier-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        protocol_id=protocol.protocol_id,
        earlier_outcome_id=outcomes[0].outcome_id,
        later_outcome_id=outcomes[1].outcome_id,
        created_at=NOW + timedelta(minutes=45),
        idempotency_key="candidate-dossier",
    )
    assert (
        MaterializeCandidateDossier(repository).execute(
            dossier_id=dossier.dossier_id,
            campaign_id=FIXED_CAMPAIGN_ID,
            candidate_id=candidate.candidate_id,
            protocol_id=protocol.protocol_id,
            earlier_outcome_id=outcomes[0].outcome_id,
            later_outcome_id=outcomes[1].outcome_id,
            created_at=NOW + timedelta(hours=1),
            idempotency_key="candidate-dossier-retry",
        )
        is dossier
    )
    evaluation = ApplyCandidateNominationPolicy(repository).execute(
        evaluation_id="btc-monthly-200-nomination-evaluation-v1",
        dossier_id=dossier.dossier_id,
        nomination_event_id="btc-monthly-200-nominated-event-v1",
        development_closed_event_id="btc-monthly-200-development-closed-event-v1",
        actor="test",
        reason="Apply the exact retained policy without discretion.",
        evaluated_at=NOW + timedelta(minutes=46),
        idempotency_key="candidate-nomination-policy",
    )
    assert dossier.passing_windows + dossier.failing_windows + dossier.inconclusive_windows == 2
    assert len(str(dossier.compounded_conservative_return).split(".")[-1]) <= 8
    workspace = candidate_workspace(repository)
    assert workspace["approved_champion_status"] == NO_APPROVED_CHAMPION
    assert workspace["financial_authority"] == "NONE"
    assert not workspace["paper_eligible"] and not workspace["live_eligible"]
    assert workspace["campaign_budget"].consumed_candidates == 1
    assert workspace["campaign_budget"].consumed_experiments == 2
    assert workspace["candidate_1_nomination_eligible"] is evaluation.eligible


def _repository_with_candidate_1() -> InMemoryCryptoCampaignCandidate2Repository:
    repository = _campaign_candidate_repository()
    assert isinstance(repository, InMemoryCryptoCampaignCandidate2Repository)
    hypothesis, _ = RegisterCandidateHypothesis(repository).execute(
        hypothesis_id="candidate-1-hypothesis",
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=30),
        idempotency_key="candidate-1-hypothesis",
    )
    strategy, _ = RegisterCandidateStrategy(repository).execute(
        specification_id="btc-usd-monthly-200-observation-price-trend-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        created_at=NOW + timedelta(minutes=31),
        idempotency_key="candidate-1-strategy",
    )
    candidate = RegisterCandidate(repository).execute(
        candidate_id="btc-usd-monthly-200-observation-price-trend-candidate-1-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(minutes=32),
        idempotency_key="candidate-1-register",
    )
    protocol = RegisterCandidateProtocol(repository).execute(
        protocol_id="candidate-1-protocol",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        code_revision=REVISION,
        created_at=NOW + timedelta(minutes=33),
        idempotency_key="candidate-1-protocol",
    )
    runs = tuple(
        ExecuteCandidateWindow(repository).execute(
            run_id=(
                "btc-usd-monthly-200-earlier-development-v1"
                if index == 0
                else "btc-usd-monthly-200-later-development-v1"
            ),
            consumption_id=f"candidate-1-consumption-{index}",
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            actor="test",
            executed_at=NOW + timedelta(minutes=34 + index),
            idempotency_key=f"candidate-1-window-{index}",
        )
        for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID))
    )
    outcomes = tuple(
        RecordCandidateOutcome(repository).execute(
            outcome_id=f"candidate-1-outcome-{index}",
            run_id=run.run_id,
            actor="test",
            decided_at=NOW + timedelta(minutes=40 + index),
            idempotency_key=f"candidate-1-outcome-{index}",
        )
        for index, run in enumerate(runs)
    )
    dossier = MaterializeCandidateDossier(repository).execute(
        dossier_id="candidate-1-dossier",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        protocol_id=protocol.protocol_id,
        earlier_outcome_id=outcomes[0].outcome_id,
        later_outcome_id=outcomes[1].outcome_id,
        created_at=NOW + timedelta(minutes=42),
        idempotency_key="candidate-1-dossier",
    )
    evaluation = ApplyCandidateNominationPolicy(repository).execute(
        evaluation_id="candidate-1-nomination",
        dossier_id=dossier.dossier_id,
        nomination_event_id="candidate-1-nominated",
        development_closed_event_id="candidate-1-development-closed",
        actor="test",
        reason="Apply exact policy.",
        evaluated_at=NOW + timedelta(minutes=43),
        idempotency_key="candidate-1-nomination",
    )
    assert not evaluation.eligible
    return repository


def test_campaign_candidate_2_two_window_budget_dossier_and_rejection() -> None:
    repository = _repository_with_candidate_1()
    hypothesis, specification = RegisterBreakoutHypothesis(repository).execute(
        hypothesis_id="candidate-2-breakout-hypothesis",
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=50),
        idempotency_key="candidate-2-hypothesis",
    )
    strategy, comparisons = RegisterBreakoutStrategy(repository).execute(
        specification_id="btc-usd-monthly-stateful-90-45-breakout-v1",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        actor="test",
        created_at=NOW + timedelta(minutes=51),
        idempotency_key="candidate-2-strategy",
    )
    assert specification.initial_state is BreakoutPositionState.CASH
    assert comparisons.against_sma_20_100.materially_distinct
    assert comparisons.against_monthly_252.materially_distinct
    assert comparisons.against_monthly_200.materially_distinct
    candidate = RegisterBreakoutCandidate(repository).execute(
        candidate_id="btc-usd-monthly-stateful-90-45-breakout-candidate-2-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(minutes=52),
        idempotency_key="candidate-2-register",
    )
    retry = RegisterBreakoutCandidate(repository).execute(
        candidate_id=candidate.candidate_id,
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(hours=2),
        idempotency_key="candidate-2-register-retry",
    )
    assert retry.canonical_hash == candidate.canonical_hash
    protocol = RegisterBreakoutProtocol(repository).execute(
        protocol_id="candidate-2-breakout-protocol-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        code_revision=REVISION,
        created_at=NOW + timedelta(minutes=53),
        idempotency_key="candidate-2-protocol",
    )
    runs = tuple(
        ExecuteBreakoutWindow(repository).execute(
            run_id=f"candidate-2-breakout-window-{index}",
            consumption_id=f"candidate-2-consumption-{index}",
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            actor="test",
            executed_at=NOW + timedelta(minutes=54 + index),
            idempotency_key=f"candidate-2-window-{index}",
        )
        for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID))
    )
    before_replay = len(repository.campaign_experiments(FIXED_CAMPAIGN_ID))
    assert ExecuteBreakoutWindow(repository).replay(runs[0].run_id) is runs[0]
    assert len(repository.campaign_experiments(FIXED_CAMPAIGN_ID)) == before_replay == 4
    assert all(run.initial_state is BreakoutPositionState.CASH for run in runs)
    assert all(len(run.prior_comparisons) == 3 for run in runs)
    outcomes = tuple(
        RecordBreakoutOutcome(repository).execute(
            outcome_id=f"candidate-2-outcome-{index}",
            run_id=run.run_id,
            actor="test",
            decided_at=NOW + timedelta(minutes=60 + index),
            idempotency_key=f"candidate-2-outcome-{index}",
        )
        for index, run in enumerate(runs)
    )
    dossier = MaterializeBreakoutDossier(repository).execute(
        dossier_id="candidate-2-dossier-v1",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        protocol_id=protocol.protocol_id,
        earlier_outcome_id=outcomes[0].outcome_id,
        later_outcome_id=outcomes[1].outcome_id,
        created_at=NOW + timedelta(minutes=63),
        idempotency_key="candidate-2-dossier",
    )
    evaluation = ApplyBreakoutNominationPolicy(repository).execute(
        evaluation_id="candidate-2-nomination-v1",
        dossier_id=dossier.dossier_id,
        nomination_event_id="candidate-2-nominated",
        development_closed_event_id="candidate-2-development-closed",
        actor="test",
        reason="Apply the exact Candidate 2 policy without discretion.",
        evaluated_at=NOW + timedelta(minutes=64),
        idempotency_key="candidate-2-nomination",
    )
    assert not evaluation.eligible
    assert "BOTH_WINDOWS_PASS" in evaluation.failed_conditions
    assert dossier.passing_windows + dossier.failing_windows == 2
    assert not dossier.holdout_accessed
    assert not dossier.network_request_performed
    workspace = candidate_2_workspace(repository)
    budget = workspace["campaign_budget"]
    assert budget.consumed_candidates == 2
    assert budget.consumed_experiments == 4
    assert workspace["campaign_state"].value == "DEVELOPMENT_ACTIVE"
    assert workspace["leading_challenger_status"] == "NO_LEADING_CHALLENGER"
    assert workspace["approved_champion_status"] == NO_APPROVED_CHAMPION
    assert workspace["financial_authority"] == "NONE"


def _replace_with_breakout_nomination_acceptance_prices(
    repository: InMemoryCryptoCampaignCandidate2Repository,
) -> None:
    for snapshot_id, validation_start in (
        (EARLIER_SNAPSHOT_ID, EARLIER_VALIDATION_START),
        (LATER_SNAPSHOT_ID, LATER_VALIDATION_START),
    ):
        snapshot = repository.snapshot(snapshot_id)
        assert snapshot is not None
        candles = repository.candles(snapshot.capture_id)
        reference_start = validation_start - timedelta(days=91)
        replacements = []
        for candle in candles:
            day = candle.bucket_start
            if day < reference_start:
                open_price = close = Decimal(100)
            elif day < validation_start - timedelta(days=1):
                offset = Decimal((day - reference_start).days)
                close = Decimal(100) + offset * Decimal(50) / Decimal(89)
                close = close.quantize(Decimal("0.00000001"))
                open_price = close
            elif day == validation_start - timedelta(days=1):
                open_price = close = Decimal(160)
            elif day < validation_start + timedelta(days=30):
                offset = Decimal((day - validation_start).days)
                close = Decimal(160) + offset * Decimal(60) / Decimal(29)
                close = close.quantize(Decimal("0.00000001"))
                open_price = close
            elif day == validation_start + timedelta(days=30):
                open_price = Decimal(220)
                close = Decimal(80)
            elif day == validation_start + timedelta(days=31):
                open_price = Decimal(180)
                close = Decimal(20)
            else:
                open_price = close = Decimal(20)
            replacements.append(
                CryptoDailyCandle.create(
                    capture_id=candle.capture_id,
                    attempt_id=candle.attempt_id,
                    evidence_class=candle.evidence_class,
                    bucket_start=day,
                    open=open_price,
                    high=max(open_price, close) + Decimal(1),
                    low=min(open_price, close) - Decimal(1),
                    close=close,
                    volume=candle.volume,
                    ingested_at=candle.ingested_at,
                    raw_response_hash=candle.raw_response_hash,
                )
            )
        repository._candles[snapshot.capture_id] = replacements


def test_campaign_candidate_2_nomination_eligible_branch_never_releases_holdout() -> None:
    repository = _repository_with_candidate_1()
    _replace_with_breakout_nomination_acceptance_prices(repository)
    hypothesis, _ = RegisterBreakoutHypothesis(repository).execute(
        hypothesis_id="candidate-2-eligible-hypothesis",
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=50),
        idempotency_key="candidate-2-eligible-hypothesis",
    )
    strategy, _ = RegisterBreakoutStrategy(repository).execute(
        specification_id="candidate-2-eligible-breakout-strategy",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        actor="test",
        created_at=NOW + timedelta(minutes=51),
        idempotency_key="candidate-2-eligible-strategy",
    )
    candidate = RegisterBreakoutCandidate(repository).execute(
        candidate_id="candidate-2-eligible-breakout",
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(minutes=52),
        idempotency_key="candidate-2-eligible-register",
    )
    protocol = RegisterBreakoutProtocol(repository).execute(
        protocol_id="candidate-2-eligible-protocol",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        code_revision=REVISION,
        created_at=NOW + timedelta(minutes=53),
        idempotency_key="candidate-2-eligible-protocol",
    )
    runs = tuple(
        ExecuteBreakoutWindow(repository).execute(
            run_id=f"candidate-2-eligible-run-{index}",
            consumption_id=f"candidate-2-eligible-consumption-{index}",
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            actor="test",
            executed_at=NOW + timedelta(minutes=54 + index),
            idempotency_key=f"candidate-2-eligible-run-{index}",
        )
        for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID))
    )
    outcomes = tuple(
        RecordBreakoutOutcome(repository).execute(
            outcome_id=f"candidate-2-eligible-outcome-{index}",
            run_id=run.run_id,
            actor="test",
            decided_at=NOW + timedelta(minutes=60 + index),
            idempotency_key=f"candidate-2-eligible-outcome-{index}",
        )
        for index, run in enumerate(runs)
    )
    assert all(
        outcome.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE for outcome in outcomes
    )
    dossier = MaterializeBreakoutDossier(repository).execute(
        dossier_id="candidate-2-eligible-dossier",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        protocol_id=protocol.protocol_id,
        earlier_outcome_id=outcomes[0].outcome_id,
        later_outcome_id=outcomes[1].outcome_id,
        created_at=NOW + timedelta(minutes=63),
        idempotency_key="candidate-2-eligible-dossier",
    )
    evaluation = ApplyBreakoutNominationPolicy(repository).execute(
        evaluation_id="candidate-2-eligible-nomination",
        dossier_id=dossier.dossier_id,
        nomination_event_id="candidate-2-eligible-nominated",
        development_closed_event_id="candidate-2-eligible-development-closed",
        actor="test",
        reason="Every exact deterministic acceptance condition passed.",
        evaluated_at=NOW + timedelta(minutes=64),
        idempotency_key="candidate-2-eligible-nomination",
    )
    assert evaluation.eligible, evaluation.failed_conditions
    assert not evaluation.failed_conditions
    workspace = candidate_2_workspace(repository)
    assert workspace["campaign_state"].value == "DEVELOPMENT_CLOSED"
    assert (
        workspace["leading_challenger_status"]
        == "NOMINATED_RESEARCH_CHALLENGER_REQUIRES_SEALED_HOLDOUT_GOVERNANCE"
    )
    assert workspace["holdout_non_use"]["state"].value == "SEALED"
    assert workspace["holdout_non_use"]["release_authorized"] is False
    assert workspace["holdout_non_use"]["evaluated"] is False
    assert workspace["financial_authority"] == "NONE"


def _replace_with_nomination_acceptance_prices(
    repository: InMemoryCryptoCampaignCandidateRepository,
) -> None:
    for snapshot_id, validation_start in (
        (EARLIER_SNAPSHOT_ID, EARLIER_VALIDATION_START),
        (LATER_SNAPSHOT_ID, LATER_VALIDATION_START),
    ):
        snapshot = repository.snapshot(snapshot_id)
        assert snapshot is not None
        candles = repository.candles(snapshot.capture_id)
        warmup_days = (validation_start - candles[0].bucket_start).days
        replacements = []
        for candle in candles:
            day = candle.bucket_start
            if day < validation_start:
                offset = (day - candles[0].bucket_start).days
                close = Decimal(100) + Decimal(20) * Decimal(offset) / Decimal(warmup_days - 1)
                close = close.quantize(Decimal("0.00000001"))
                open_price = close
            elif day < validation_start + timedelta(days=31):
                offset = (day - validation_start).days
                close = Decimal(120) + Decimal(80) * Decimal(offset) / Decimal(30)
                close = close.quantize(Decimal("0.00000001"))
                open_price = close
            elif day < validation_start + timedelta(days=62):
                open_price = close = Decimal(200)
            elif day < validation_start + timedelta(days=92):
                open_price = close = Decimal(180)
            elif day < validation_start + timedelta(days=123):
                open_price = close = Decimal(150)
            else:
                open_price = (
                    Decimal(150) if day == validation_start + timedelta(days=123) else Decimal(50)
                )
                close = Decimal(50)
            replacements.append(
                CryptoDailyCandle.create(
                    capture_id=candle.capture_id,
                    attempt_id=candle.attempt_id,
                    evidence_class=candle.evidence_class,
                    bucket_start=day,
                    open=open_price,
                    high=max(open_price, close) + Decimal(1),
                    low=min(open_price, close) - Decimal(1),
                    close=close,
                    volume=candle.volume,
                    ingested_at=candle.ingested_at,
                    raw_response_hash=candle.raw_response_hash,
                )
            )
        repository._candles[snapshot.capture_id] = replacements


def test_candidate_nomination_acceptance_data_closes_development_without_release() -> None:
    repository = _campaign_candidate_repository()
    _replace_with_nomination_acceptance_prices(repository)
    hypothesis, _ = RegisterCandidateHypothesis(repository).execute(
        hypothesis_id="acceptance-candidate-hypothesis",
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="test",
        created_at=NOW + timedelta(minutes=30),
        idempotency_key="acceptance-hypothesis",
    )
    strategy, _ = RegisterCandidateStrategy(repository).execute(
        specification_id="acceptance-monthly-200-strategy",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        created_at=NOW + timedelta(minutes=31),
        idempotency_key="acceptance-strategy",
    )
    candidate = RegisterCandidate(repository).execute(
        candidate_id="acceptance-monthly-200-candidate",
        campaign_id=FIXED_CAMPAIGN_ID,
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        strategy_specification_id=strategy.specification_id,
        actor="test",
        registered_at=NOW + timedelta(minutes=32),
        idempotency_key="acceptance-candidate",
    )
    protocol = RegisterCandidateProtocol(repository).execute(
        protocol_id="acceptance-monthly-200-protocol",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        code_revision=REVISION,
        created_at=NOW + timedelta(minutes=33),
        idempotency_key="acceptance-protocol",
    )
    runs = tuple(
        ExecuteCandidateWindow(repository).execute(
            run_id=f"acceptance-window-{index}",
            consumption_id=f"acceptance-consumption-{index}",
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot_id,
            code_revision=REVISION,
            actor="test",
            executed_at=NOW + timedelta(minutes=34 + index),
            idempotency_key=f"acceptance-window-key-{index}",
        )
        for index, snapshot_id in enumerate((EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID))
    )
    assert all(
        candidate_metric(run.metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND).net_return >= 0
        for run in runs
    )
    outcomes = tuple(
        RecordCandidateOutcome(repository).execute(
            outcome_id=f"acceptance-outcome-{index}",
            run_id=run.run_id,
            actor="test",
            decided_at=NOW + timedelta(minutes=40 + index),
            idempotency_key=f"acceptance-outcome-key-{index}",
        )
        for index, run in enumerate(runs)
    )
    assert all(
        outcome.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE for outcome in outcomes
    )
    dossier = MaterializeCandidateDossier(repository).execute(
        dossier_id="acceptance-candidate-dossier",
        campaign_id=FIXED_CAMPAIGN_ID,
        candidate_id=candidate.candidate_id,
        protocol_id=protocol.protocol_id,
        earlier_outcome_id=outcomes[0].outcome_id,
        later_outcome_id=outcomes[1].outcome_id,
        created_at=NOW + timedelta(minutes=45),
        idempotency_key="acceptance-dossier",
    )
    evaluation = ApplyCandidateNominationPolicy(repository).execute(
        evaluation_id="acceptance-nomination-evaluation",
        dossier_id=dossier.dossier_id,
        nomination_event_id="acceptance-candidate-nominated",
        development_closed_event_id="acceptance-development-closed",
        actor="test",
        reason="All exact deterministic acceptance conditions passed.",
        evaluated_at=NOW + timedelta(minutes=46),
        idempotency_key="acceptance-nomination",
    )
    workspace = candidate_workspace(repository)
    assert evaluation.eligible
    assert workspace["campaign_state"].value == "DEVELOPMENT_CLOSED"
    assert workspace["leading_challenger_status"].startswith("NOMINATED_RESEARCH")
    assert workspace["holdout_non_use"]["release_authorized"] is False
    assert workspace["holdout_non_use"]["evaluated"] is False


def test_candidate_exact_id_cli_and_read_api_surface() -> None:
    parsed = parser().parse_args(
        (
            "crypto-candidate-window-run",
            "--run-id",
            "r",
            "--consumption-id",
            "c",
            "--protocol-id",
            "p",
            "--snapshot-id",
            "s",
            "--actor",
            "a",
            "--idempotency-key",
            "k",
        )
    )
    assert parsed.command == "crypto-candidate-window-run"
    methods = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", set())
    }
    assert ("/api/v1/crypto-research/candidate-runs/{run_id}", "GET") in methods
    assert ("/api/v1/crypto-research/candidate-dossiers/{dossier_id}", "GET") in methods
