from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.coinbase_public import FixtureCoinbaseCandleSource
from apex.application.crypto_dossier import (
    ExecuteCryptoDisjointReplication,
    InMemoryCryptoDossierRepository,
    MaterializeCryptoEvidenceDossier,
    RecordCryptoDossierConclusion,
    RecordCryptoReplicationOutcome,
    RegisterCryptoReplicationProtocol,
    RegisterFrozenCryptoStrategy,
    crypto_dossier_workspace,
    verify_original_rejection,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    CryptoResearchConflict,
    ExecuteCryptoTrendValidation,
    MaterializeCryptoHistoricalSnapshot,
    RecordCryptoExperimentOutcome,
    RegisterCryptoHypothesis,
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
    CryptoDossierConclusionType,
    CryptoStrategySpecification,
    frozen_semantics_payload,
)
from apex.domain.crypto_research import (
    FIXED_DATA_END,
    FIXED_DATA_START,
    RESEARCH_METRIC_QUANTUM,
    CostScenarioName,
    PortfolioName,
    expected_bucket_starts,
)
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
)
from apex.operator_api import app
from scripts.dev import parser

NOW = datetime(2026, 7, 29, 18, tzinfo=UTC)


def _complete_capture(
    repository: InMemoryCryptoDossierRepository,
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


def _repository_with_original() -> InMemoryCryptoDossierRepository:
    repository = InMemoryCryptoDossierRepository()
    hypothesis, hypothesis_specification = RegisterCryptoHypothesis(repository).execute(
        hypothesis_id="test-original",
        revision=1,
        actor="test",
        created_at=NOW,
    )
    capture = _complete_capture(
        repository,
        capture_id="original-fixture-capture",
        start=FIXED_DATA_START,
        end=FIXED_DATA_END,
    )
    snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="original-fixture-snapshot",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    run = ExecuteCryptoTrendValidation(repository).execute(
        run_id="original-fixture-run",
        hypothesis_version_id=hypothesis.hypothesis_version_id,
        snapshot_id=snapshot.snapshot_id,
        code_revision="a" * 40,
        executed_at=NOW,
    )
    outcome = RecordCryptoExperimentOutcome(repository).execute(
        outcome_id="original-fixture-outcome",
        run_id=run.run_id,
        actor="test",
        decided_at=NOW,
    )
    retained_hypothesis = replace(
        hypothesis,
        hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
    )
    retained_snapshot = replace(
        snapshot,
        snapshot_id=ORIGINAL_SNAPSHOT_ID,
        manifest_hash=ORIGINAL_SNAPSHOT_MANIFEST_HASH,
    )
    retained_run = replace(
        run,
        run_id=ORIGINAL_RUN_ID,
        hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
        snapshot_id=ORIGINAL_SNAPSHOT_ID,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        result_hash=ORIGINAL_RESULT_HASH,
        canonical_hash=ORIGINAL_CANONICAL_RUN_HASH,
    )
    retained_outcome = replace(
        outcome,
        outcome_id=ORIGINAL_OUTCOME_ID,
        run_id=ORIGINAL_RUN_ID,
        decision=ExperimentOutcomeDecision.REJECTED,
        paper_eligible=False,
        live_eligible=False,
        canonical_hash=ORIGINAL_OUTCOME_HASH,
    )
    repository._hypotheses[ORIGINAL_HYPOTHESIS_VERSION_ID] = retained_hypothesis
    repository._specifications[ORIGINAL_HYPOTHESIS_VERSION_ID] = replace(
        hypothesis_specification,
        hypothesis_version_id=ORIGINAL_HYPOTHESIS_VERSION_ID,
    )
    repository._snapshots[ORIGINAL_SNAPSHOT_ID] = retained_snapshot
    repository._runs[ORIGINAL_RUN_ID] = retained_run
    repository._outcomes[ORIGINAL_OUTCOME_ID] = retained_outcome
    return repository


def _replication_fixture(repository: InMemoryCryptoDossierRepository):
    capture = _complete_capture(
        repository,
        capture_id="replication-fixture-capture",
        start=REPLICATION_DATA_START,
        end=REPLICATION_DATA_END,
    )
    snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id="replication-fixture-snapshot",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    strategy = RegisterFrozenCryptoStrategy(repository).execute(
        specification_id="btc-sma-20-100-rejected-v1",
        created_at=NOW,
    )
    protocol = RegisterCryptoReplicationProtocol(repository).execute(
        protocol_id="btc-sma-disjoint-replication-v1",
        strategy_specification_id=strategy.specification_id,
        created_at=NOW,
    )
    run = ExecuteCryptoDisjointReplication(repository).execute(
        run_id="btc-sma-replication-fixture-v1",
        protocol_id=protocol.protocol_id,
        snapshot_id=snapshot.snapshot_id,
        code_revision="b" * 40,
        executed_at=NOW,
    )
    return strategy, protocol, snapshot, run


def test_original_rejection_and_hashes_are_frozen() -> None:
    repository = _repository_with_original()
    verify_original_rejection(repository)
    assert repository.run(ORIGINAL_RUN_ID).result_hash == ORIGINAL_RESULT_HASH
    assert repository.run(ORIGINAL_RUN_ID).canonical_hash == ORIGINAL_CANONICAL_RUN_HASH
    assert repository.outcome(ORIGINAL_OUTCOME_ID).decision is ExperimentOutcomeDecision.REJECTED


def test_strategy_semantics_are_exact_and_duplicate_registration_is_negative_knowledge() -> None:
    repository = _repository_with_original()
    strategy = RegisterFrozenCryptoStrategy(repository).execute(
        specification_id="btc-sma-20-100-rejected-v1",
        created_at=NOW,
    )
    assert strategy.semantics_hash
    assert dict(strategy.semantics)["fast_sma_days"] == 20
    assert dict(strategy.semantics)["slow_sma_days"] == 100
    assert dict(strategy.semantics)["fee_bps_per_side"] == Decimal("60")
    assert dict(strategy.semantics)["slippage_bps_per_side"] == Decimal("10")
    assert (
        dict(strategy.semantics)["expected_holding_behavior"]
        == "HOLD_LONG_UNTIL_PRIOR_FAST_SMA_NOT_ABOVE_PRIOR_SLOW_SMA"
    )
    assert not strategy.paper_eligible and not strategy.live_eligible
    with pytest.raises(CryptoResearchConflict, match="DUPLICATE_STRATEGY_ALREADY_TESTED"):
        RegisterFrozenCryptoStrategy(repository).execute(
            specification_id="unrelated-duplicate",
            created_at=NOW,
        )


def test_material_change_requires_parent_rationale_and_new_semantics() -> None:
    parent = CryptoStrategySpecification.create(
        specification_id="parent",
        version=1,
        created_at=NOW,
        semantics=frozen_semantics_payload(),
    )
    with pytest.raises(ValueError, match="CHANGE_RATIONALE_REQUIRED"):
        CryptoStrategySpecification.create(
            specification_id="child",
            version=2,
            parent_specification_id=parent.specification_id,
            created_at=NOW,
            semantics=frozen_semantics_payload(fast_sma_days=21),
        )
    child = CryptoStrategySpecification.create(
        specification_id="child",
        version=2,
        parent_specification_id=parent.specification_id,
        change_rationale="Predeclared 21-day challenger; not this replication.",
        created_at=NOW,
        semantics=frozen_semantics_payload(fast_sma_days=21),
    )
    assert child.semantics_hash != parent.semantics_hash


def test_disjoint_protocol_counts_leap_day_and_keeps_semantics() -> None:
    repository = _repository_with_original()
    strategy, protocol, _, run = _replication_fixture(repository)
    assert len(expected_bucket_starts(protocol.data_start, protocol.data_end)) == 731
    assert len(expected_bucket_starts(protocol.validation_start, protocol.validation_end)) == 366
    assert protocol.data_end == FIXED_DATA_START
    assert protocol.strategy_semantics_hash == strategy.semantics_hash
    assert protocol.baseline_definitions == ("CASH", "BUY_AND_HOLD")
    assert protocol.conclusion_policy == "CRYPTO_DOSSIER_CONCLUSION_POLICY_V1"
    assert run.strategy_semantics_hash == strategy.semantics_hash
    assert len(run.signals) == 366
    assert all(
        signal.information_through == signal.signal_for - timedelta(days=1)
        for signal in run.signals
    )
    replay = ExecuteCryptoDisjointReplication(repository).replay(run.run_id)
    assert replay.result_hash == run.result_hash
    assert replay.canonical_hash == run.canonical_hash


def test_fixture_replication_is_inconclusive_and_has_no_authority() -> None:
    repository = _repository_with_original()
    _, _, _, run = _replication_fixture(repository)
    outcome = RecordCryptoReplicationOutcome(repository).execute(
        outcome_id="fixture-replication-outcome",
        run_id=run.run_id,
        actor="test",
        decided_at=NOW,
    )
    assert outcome.decision is ExperimentOutcomeDecision.DEFERRED
    dossier = MaterializeCryptoEvidenceDossier(repository).execute(
        dossier_id="fixture-dossier",
        replication_outcome_id=outcome.outcome_id,
        created_at=NOW,
    )
    conclusion = RecordCryptoDossierConclusion(repository).execute(
        conclusion_id="fixture-conclusion",
        dossier_id=dossier.dossier_id,
        actor="test",
        decided_at=NOW,
    )
    assert conclusion.conclusion is CryptoDossierConclusionType.INCONCLUSIVE
    workspace = crypto_dossier_workspace(repository)
    assert workspace["approved_champion_status"] == NO_APPROVED_CHAMPION
    assert workspace["orders"] == workspace["positions"] == 0
    assert workspace["reservations"] == workspace["broker_writes"] == 0
    assert workspace["journals"] == 0


def test_complete_genuine_failure_confirms_rejection_and_dossier_membership() -> None:
    repository = _repository_with_original()
    strategy, protocol, snapshot, fixture_run = _replication_fixture(repository)
    original_trend_return = next(
        metric.net_return
        for metric in repository.run(ORIGINAL_RUN_ID).metrics
        if metric.portfolio is PortfolioName.TREND_RULE
        and metric.scenario is CostScenarioName.CONSERVATIVE_RESEARCH
    )
    genuine_metrics = tuple(
        replace(metric, net_return=original_trend_return + RESEARCH_METRIC_QUANTUM)
        if metric.portfolio is PortfolioName.TREND_RULE
        and metric.scenario is CostScenarioName.CONSERVATIVE_RESEARCH
        else metric
        for metric in fixture_run.metrics
    )
    genuine_run = replace(
        fixture_run,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        metrics=genuine_metrics,
    )
    repository._replication_runs[genuine_run.run_id] = genuine_run
    outcome = RecordCryptoReplicationOutcome(repository).execute(
        outcome_id="genuine-replication-outcome",
        run_id=genuine_run.run_id,
        actor="test",
        decided_at=NOW,
    )
    assert outcome.decision is ExperimentOutcomeDecision.REJECTED
    dossier = MaterializeCryptoEvidenceDossier(repository).execute(
        dossier_id="genuine-dossier",
        replication_outcome_id=outcome.outcome_id,
        created_at=NOW,
    )
    assert dossier.strategy_specification_id == strategy.specification_id
    assert dossier.replication_protocol_id == protocol.protocol_id
    assert dossier.replication_snapshot_id == snapshot.snapshot_id
    assert dossier.original_result_hash == ORIGINAL_RESULT_HASH
    assert dossier.median_conservative_net_return == (
        original_trend_return + (RESEARCH_METRIC_QUANTUM / Decimal(2))
    ).quantize(RESEARCH_METRIC_QUANTUM)
    conclusion = RecordCryptoDossierConclusion(repository).execute(
        conclusion_id="genuine-conclusion",
        dossier_id=dossier.dossier_id,
        actor="test",
        decided_at=NOW,
    )
    assert conclusion.conclusion is CryptoDossierConclusionType.REJECTION_CONFIRMED


def test_complete_genuine_policy_pass_produces_mixed_evidence_not_champion() -> None:
    repository = _repository_with_original()
    _, _, _, fixture_run = _replication_fixture(repository)
    metrics = []
    for metric in fixture_run.metrics:
        if (
            metric.portfolio is PortfolioName.TREND_RULE
            and metric.scenario is CostScenarioName.CONSERVATIVE_RESEARCH
        ):
            metric = replace(
                metric,
                net_return=Decimal("0.01"),
                maximum_drawdown=Decimal("0.01"),
                completed_round_trips=1,
            )
        metrics.append(metric)
    mixed_run = replace(
        fixture_run,
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        metrics=tuple(metrics),
    )
    repository._replication_runs[mixed_run.run_id] = mixed_run
    outcome = RecordCryptoReplicationOutcome(repository).execute(
        outcome_id="mixed-outcome",
        run_id=mixed_run.run_id,
        actor="test",
        decided_at=NOW,
    )
    assert outcome.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE
    dossier = MaterializeCryptoEvidenceDossier(repository).execute(
        dossier_id="mixed-dossier",
        replication_outcome_id=outcome.outcome_id,
        created_at=NOW,
    )
    conclusion = RecordCryptoDossierConclusion(repository).execute(
        conclusion_id="mixed-conclusion",
        dossier_id=dossier.dossier_id,
        actor="test",
        decided_at=NOW,
    )
    assert conclusion.conclusion is CryptoDossierConclusionType.MIXED_EVIDENCE
    assert not conclusion.paper_eligible and not conclusion.live_eligible


def test_dossier_cli_and_read_only_api_surfaces_are_exact_id_only() -> None:
    run = parser().parse_args(
        [
            "crypto-replication-run",
            "--run-id",
            "replication-one",
            "--protocol-id",
            "protocol-one",
            "--snapshot-id",
            "snapshot-one",
        ]
    )
    assert run.run_id == "replication-one"
    materialize = parser().parse_args(
        [
            "crypto-dossier-materialize",
            "--dossier-id",
            "dossier-one",
            "--replication-outcome-id",
            "outcome-one",
        ]
    )
    assert materialize.dossier_id == "dossier-one"
    methods = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/crypto-research/strategies/{specification_id}", "GET") in methods
    assert (
        "/api/v1/crypto-research/replication-protocols/{protocol_id}",
        "GET",
    ) in methods
    assert ("/api/v1/crypto-research/replications/{run_id}", "GET") in methods
    assert ("/api/v1/crypto-research/dossiers/{dossier_id}", "GET") in methods
    assert ("/api/v1/crypto-research/approved-champion", "GET") in methods
