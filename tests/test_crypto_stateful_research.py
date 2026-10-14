from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.crypto_stateful_research import (
    CryptoStatefulExperimentExecutor,
    ImplementCryptoStatefulMethod,
    InMemoryCryptoStatefulResearchRepository,
    RefineAndApproveGeneratedCryptoProposal,
)
from apex.application.research_operations import (
    CreateResearchProposal,
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    InMemoryResearchOperationsRepository,
    RefreshResearchAgenda,
)
from apex.domain.crypto_stateful_research import (
    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
    CryptoExecutionCandle,
    CryptoExecutionHistory,
    CryptoState,
    CryptoStatefulAction,
    CryptoStatefulMethodSpecification,
    run_stateful_crypto_experiment,
)
from apex.domain.research_operations import (
    DuplicateClassification,
    ResearchAgendaSourceFact,
    ResearchFeasibilityState,
    ResearchProposal,
    ResearchProposalOrigin,
    ResearchVenue,
)

NOW = datetime(2026, 8, 3, 17, tzinfo=UTC)
HASH = "a" * 64


def _original_proposal() -> ResearchProposal:
    return ResearchProposal.create(
        proposal_id="rank-1-generated-crypto",
        version=1,
        agenda_item_id="agenda-negative-crypto",
        venue=ResearchVenue.CRYPTO,
        hypothesis="A stateful challenger may resolve negative current economics.",
        expected_mechanism="Stateful monthly transitions replace short-horizon ranking.",
        instruments=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
        proposed_family="CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1_MATERIAL_STATEFUL_CHALLENGER",
        definition_kind="CRYPTO_GENERIC_LONG_CASH",
        strategy_definition=(
            ("decision_cadence", "CALENDAR_MONTHLY"),
            ("execution", "NEXT_OPEN"),
            ("feature", "POINT_TO_POINT_RETURN"),
            ("state", "STATEFUL_LONG_CASH"),
        ),
        exact_change_from_prior=(
            "STATEFUL_MONTHLY_REGIME_TRANSITIONS_REPLACE_SHORT_HORIZON_REVERSAL_RANKING"
        ),
        prior_research_ids=(),
        required_data_ids=("negative-expected-economics",),
        experiment_configuration=(("scope", "RETAINED_DEVELOPMENT_ONLY"),),
        validation_windows=(("scope", "EXACT_RETAINED_OR_DISJOINT_SCOPE"),),
        cost_assumptions=(("cost_policy", "EXISTING_FIXED_COST_ASSUMPTIONS"),),
        success_criteria=("DECISION_EVIDENCE_IMPROVED",),
        failure_criteria=("ECONOMICS_OR_QUALITY_FAILED",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.SYSTEM_HEURISTIC,
        actor="operator:generator",
        warnings=("RESEARCH_ONLY",),
        created_at=NOW - timedelta(days=1),
    )


def test_exact_refinement_preserves_original_and_is_materially_distinct() -> None:
    operations = InMemoryResearchOperationsRepository()
    stateful = InMemoryCryptoStatefulResearchRepository()
    fact = ResearchAgendaSourceFact.create(
        fact_id="negative-crypto",
        fact_type="NEGATIVE_EXPECTED_ECONOMICS",
        venue=ResearchVenue.CRYPTO,
        market_scope=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
        state="BLOCKED",
        summary="No positive current source-eligible result.",
        evidence_ids=("negative-expected-economics",),
        evidence_hashes=(HASH,),
        observed_at=NOW - timedelta(days=1),
    )
    RefreshResearchAgenda(operations).execute(facts=(fact,))
    original = _original_proposal()
    CreateResearchProposal(operations).execute(original)
    result = RefineAndApproveGeneratedCryptoProposal(operations, stateful).execute(
        original_proposal_id=original.proposal_id,
        refined_proposal_id="rank-1-generated-crypto-refinement-v2",
        refinement_id="rank-1-generated-crypto-refinement-evidence-v1",
        dataset_id="retained-730-day-open-close-completion-v1",
        retained_observation_ids=("obs-a", "obs-b", "obs-c"),
        current_evidence_ids=("current-pulse",),
        actor="operator:test",
        refined_at=NOW,
        provider_request_limit=9,
    )
    assert operations.proposal(original.proposal_id) == original
    assert result.proposal.version == 2
    assert dict(result.proposal.strategy_definition) == {
        "decision_cadence": "DAILY_COMPLETED",
        "execution": "NEXT_ELIGIBLE_DAILY_OPEN",
        "maximum_hold_days": "14",
        "maximum_positions": "1",
        "method_id": CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
        "pullback_threshold": "-0.03",
        "return_7d": "7_COMPLETED_DAYS",
        "return_90d": "90_COMPLETED_DAYS",
        "rotation": "EXIT_TO_CASH_BEFORE_LATER_ENTRY",
        "state": "STATEFUL_LONG_ONE_ASSET_OR_CASH",
        "volatility_30d": "30_COMPLETED_DAYS_ANNUALIZED",
        "volatility_comparator": "ELIGIBLE_UNIVERSE_MEDIAN",
    }
    assert result.refinement.material_distinct
    assert result.initial_feasibility.status is ResearchFeasibilityState.REQUIRES_IMPLEMENTATION
    approval = operations.experiment_approval(result.proposal.proposal_id)
    assert approval is not None
    assert approval.experiment_budget == 1
    assert approval.provider_request_limit == 9
    assert operations.reviews(result.proposal.proposal_id)[1].duplicate_classification is (
        DuplicateClassification.MATERIALLY_DIFFERENT_CHALLENGER
    )
    assert approval.paper_authority == approval.live_authority == "NONE"


def _history(
    product_id: str,
    asset_id: str,
    closes: tuple[Decimal, ...],
    *,
    quality_ineligible_dates: tuple[datetime, ...] = (),
) -> CryptoExecutionHistory:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = tuple(
        CryptoExecutionCandle.create(
            bucket_start=start + timedelta(days=index),
            open_price=value,
            close_price=value,
        )
        for index, value in enumerate(closes)
    )
    return CryptoExecutionHistory(
        history_id=f"history-{asset_id}",
        canonical_asset_id=asset_id,
        product_id=product_id,
        source_observation_id=f"obs-{asset_id}",
        source_observation_hash=HASH,
        interval_start=start,
        interval_end=start + timedelta(days=len(closes)),
        candles=candles,
        raw_evidence_hashes=(HASH,),
        request_count=1,
        captured_at=NOW,
        canonical_hash=HASH,
        quality_ineligible_dates=quality_ineligible_dates,
    )


def _specification() -> CryptoStatefulMethodSpecification:
    return CryptoStatefulMethodSpecification(
        specification_id="stateful-spec-v1",
        proposal_id="refined-proposal-v2",
        proposal_hash=HASH,
        method_id=CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
        version=1,
        entry_lookbacks=(7, 30, 90),
        pullback_threshold=Decimal("-0.03"),
        maximum_hold_days=14,
        maximum_positions=1,
        research_notional=Decimal("10000"),
        fee_bps_per_side=Decimal("60"),
        slippage_bps_per_side=Decimal("10"),
        semantics=("NEXT_OPEN", "NO_DIRECT_ROTATION"),
        implementation_id="stateful-implementation-v1",
        implemented_at=NOW,
        canonical_hash=HASH,
    )


def _series() -> tuple[tuple[Decimal, ...], tuple[Decimal, ...], tuple[Decimal, ...]]:
    first: list[Decimal] = []
    second: list[Decimal] = []
    third: list[Decimal] = []
    for index in range(145):
        base = Decimal("100") + Decimal(index) * Decimal("0.30")
        if 96 <= index <= 103:
            base -= Decimal(index - 95) * Decimal("1.20")
        if index >= 104:
            base += Decimal(index - 103) * Decimal("1.30") - Decimal("9.60")
        first.append(base)
        second.append(
            Decimal("80")
            + Decimal(index) * Decimal("0.10")
            + (Decimal("4") if index % 2 else Decimal("-4"))
        )
        third.append(
            Decimal("60")
            + Decimal(index) * Decimal("0.08")
            + (Decimal("6") if index % 3 else Decimal("-6"))
        )
    return tuple(first), tuple(second), tuple(third)


def test_stateful_execution_uses_next_open_one_asset_and_cash_rotation() -> None:
    histories = tuple(
        _history(product, asset, values)
        for product, asset, values in zip(
            ("AAA-USD", "BBB-USD", "CCC-USD"),
            ("AAA", "BBB", "CCC"),
            _series(),
            strict=True,
        )
    )
    run = run_stateful_crypto_experiment(
        run_id="stateful-run-v1",
        proposal_id="refined-proposal-v2",
        specification=_specification(),
        dataset_id="dataset-v1",
        histories=histories,
        relative_strength_comparison=Decimal("0.01"),
        reversal_regime_comparison=Decimal("-0.02"),
        completed_at=NOW,
    )
    entries = tuple(value for value in run.decisions if value.action is CryptoStatefulAction.ENTER)
    exits = tuple(value for value in run.decisions if value.action is CryptoStatefulAction.EXIT)
    assert entries
    assert exits
    assert all(value.selected_product_id == "AAA-USD" for value in entries)
    assert all(
        value.execution_at == value.decision_at + timedelta(days=1) for value in run.decisions
    )
    assert all(value.return_7d is None or value.return_7d <= Decimal("-0.03") for value in entries)
    assert all(
        value.volatility_30d is not None
        and value.volatility_30d <= value.universe_median_volatility_30d
        for value in entries
    )
    for decision in exits:
        assert decision.resulting_state is CryptoState.CASH
        next_decision = run.decisions[decision.ordinal]
        assert next_decision.decision_at > decision.decision_at
    assert run.metrics.entry_count == len(entries)
    assert run.metrics.exit_count == len(exits)
    assert run.metrics.fees >= 0
    assert run.metrics.slippage >= 0
    assert run.authority == run.paper_authority == run.live_authority == "NONE"


def test_quality_ineligibility_forces_exit_without_direct_rotation() -> None:
    first, second, third = _series()
    start = datetime(2025, 1, 1, tzinfo=UTC)
    quality_date = start + timedelta(days=104)
    histories = (
        _history(
            "AAA-USD",
            "AAA",
            first,
            quality_ineligible_dates=(quality_date,),
        ),
        _history("BBB-USD", "BBB", second),
        _history("CCC-USD", "CCC", third),
    )
    run = run_stateful_crypto_experiment(
        run_id="stateful-quality-run-v1",
        proposal_id="refined-proposal-v2",
        specification=_specification(),
        dataset_id="dataset-v1",
        histories=histories,
        relative_strength_comparison=None,
        reversal_regime_comparison=None,
        completed_at=NOW,
    )
    quality_exits = tuple(
        value for value in run.decisions if "QUALITY_EVIDENCE_INELIGIBLE" in value.reasons
    )
    assert quality_exits
    assert all(value.action is CryptoStatefulAction.EXIT for value in quality_exits)
    assert all(value.selected_product_id is None for value in quality_exits)


def test_fourteen_day_time_stop_exits_to_cash() -> None:
    first: list[Decimal] = []
    second: list[Decimal] = []
    third: list[Decimal] = []
    for index in range(150):
        first.append(
            Decimal("100")
            + Decimal(min(index, 95)) * Decimal("0.50")
            - Decimal(max(0, index - 95)) * Decimal("0.70")
        )
        second.append(
            Decimal("90")
            + Decimal(index) * Decimal("0.10")
            + (Decimal("5") if index % 2 else Decimal("-5"))
        )
        third.append(
            Decimal("70")
            + Decimal(index) * Decimal("0.08")
            + (Decimal("7") if index % 3 else Decimal("-7"))
        )
    run = run_stateful_crypto_experiment(
        run_id="stateful-time-stop-run-v1",
        proposal_id="refined-proposal-v2",
        specification=_specification(),
        dataset_id="dataset-v1",
        histories=(
            _history("AAA-USD", "AAA", tuple(first)),
            _history("BBB-USD", "BBB", tuple(second)),
            _history("CCC-USD", "CCC", tuple(third)),
        ),
        relative_strength_comparison=None,
        reversal_regime_comparison=None,
        completed_at=NOW,
    )
    time_stops = tuple(
        value for value in run.decisions if "FOURTEEN_DAY_TIME_STOP" in value.reasons
    )
    assert time_stops
    assert all(value.holding_days == 14 for value in time_stops)
    assert all(value.resulting_state is CryptoState.CASH for value in time_stops)


def test_exact_queue_consumes_one_budget_and_replay_is_free() -> None:
    operations = InMemoryResearchOperationsRepository()
    stateful = InMemoryCryptoStatefulResearchRepository()
    RefreshResearchAgenda(operations).execute(
        facts=(
            ResearchAgendaSourceFact.create(
                fact_id="negative-crypto",
                fact_type="NEGATIVE_EXPECTED_ECONOMICS",
                venue=ResearchVenue.CRYPTO,
                market_scope=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
                state="BLOCKED",
                summary="No positive current source-eligible result.",
                evidence_ids=("negative-expected-economics",),
                evidence_hashes=(HASH,),
                observed_at=NOW - timedelta(days=1),
            ),
        )
    )
    original = _original_proposal()
    CreateResearchProposal(operations).execute(original)
    refined = RefineAndApproveGeneratedCryptoProposal(operations, stateful).execute(
        original_proposal_id=original.proposal_id,
        refined_proposal_id="queue-refinement-v2",
        refinement_id="queue-refinement-evidence-v1",
        dataset_id="queue-dataset-v1",
        retained_observation_ids=("obs-a", "obs-b", "obs-c"),
        current_evidence_ids=("current-pulse",),
        actor="operator:test",
        refined_at=NOW,
        provider_request_limit=0,
    )
    histories = tuple(
        _history(product, asset, values)
        for product, asset, values in zip(
            ("AAA-USD", "BBB-USD", "CCC-USD"),
            ("AAA", "BBB", "CCC"),
            _series(),
            strict=True,
        )
    )
    specification, feasibility, _ = ImplementCryptoStatefulMethod(operations, stateful).execute(
        proposal_id=refined.proposal.proposal_id,
        specification_id="queue-spec-v1",
        implementation_id="queue-implementation-v1",
        implemented_at=NOW + timedelta(seconds=3),
        histories=histories,
    )
    assert feasibility.status is ResearchFeasibilityState.READY_FOR_REVIEW
    queue = EnqueueResearchExperiment(operations).execute(
        queue_id="queue-v1",
        proposal_id=refined.proposal.proposal_id,
        run_id="queue-run-v1",
        actor="operator:test",
        idempotency_key="queue-key-v1",
        enqueued_at=NOW + timedelta(seconds=4),
    )
    executor = CryptoStatefulExperimentExecutor(
        stateful,
        specification,
        histories,
        "queue-dataset-v1",
        Decimal("0.01"),
        Decimal("-0.02"),
    )
    runner = ExecuteApprovedResearchQueueItem(operations, executor)
    first = runner.execute(
        queue_id=queue.item.queue_id,
        actor="operator:test",
        idempotency_key="execute-key-v1",
        executed_at=NOW + timedelta(seconds=5),
    )
    replay = runner.execute(
        queue_id=queue.item.queue_id,
        actor="operator:test",
        idempotency_key="execute-key-v1",
        executed_at=NOW + timedelta(seconds=5),
    )
    assert first.governed_result.budget.consumed_runs == 1
    assert replay.governed_result.budget.consumed_runs == 1
    assert replay.governed_result.budget.replays == 1
    assert replay.idempotent_replay
    assert not first.governed_result.recommendation.paper_authorization_created
    assert first.governed_result.recommendation.live_authority == "NONE"


def test_specification_rejects_any_threshold_change() -> None:
    original = _original_proposal()
    definition = tuple(
        (key, "-0.029" if key == "pullback_threshold" else value)
        for key, value in (
            ("decision_cadence", "DAILY_COMPLETED"),
            ("execution", "NEXT_ELIGIBLE_DAILY_OPEN"),
            ("maximum_hold_days", "14"),
            ("maximum_positions", "1"),
            ("method_id", CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1),
            ("pullback_threshold", "-0.03"),
            ("return_7d", "7_COMPLETED_DAYS"),
            ("return_90d", "90_COMPLETED_DAYS"),
            ("rotation", "EXIT_TO_CASH_BEFORE_LATER_ENTRY"),
            ("volatility_30d", "30_COMPLETED_DAYS_ANNUALIZED"),
        )
    )
    changed = ResearchProposal.create(
        proposal_id="changed",
        version=2,
        agenda_item_id=original.agenda_item_id,
        venue=original.venue,
        hypothesis=original.hypothesis,
        expected_mechanism=original.expected_mechanism,
        instruments=original.instruments,
        proposed_family=original.proposed_family,
        definition_kind=original.definition_kind,
        strategy_definition=definition,
        exact_change_from_prior="MATERIAL_CHANGED_THRESHOLD",
        prior_research_ids=(original.proposal_id,),
        required_data_ids=("data",),
        experiment_configuration=(("dataset", "data"),),
        validation_windows=(("window", "one"),),
        cost_assumptions=(("fee", "60"),),
        success_criteria=("COMPLETE",),
        failure_criteria=("FAILED",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.OPERATOR,
        actor="operator:test",
        warnings=("RESEARCH_ONLY",),
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="CRYPTO_STATEFUL_SPECIFICATION_SEMANTICS_INVALID"):
        CryptoStatefulMethodSpecification.create(
            specification_id="changed-spec",
            proposal=changed,
            implementation_id="implementation",
            implemented_at=NOW,
        )
