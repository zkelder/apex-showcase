"""PostgreSQL persistence proofs for migration 0089: the corporate-market
research agenda, generated hypotheses, the one bounded mutable agenda-run
cursor, hypothesis-execution evidence, the append-only knowledge base,
contradictions, follow-up proposals, and the agenda quality report --
round-trip, idempotent replay, immutable-evidence enforcement, mutable-
cursor updates, and downgrade protection. Every genuine trial/governance/
robustness/conclusion row a hypothesis execution references is persisted
through the exact same, unmodified migration-0079 tables the price-only
agenda already reuses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.corporate_fundamentals_postgres import (
    PostgresCorporateFundamentalsRepository,
)
from apex.adapters.corporate_market_research_postgres import (
    CORPORATE_MARKET_RESEARCH_TABLES,
    PostgresCorporateMarketResearchRepository,
)
from apex.adapters.genuine_multi_asset_history_postgres import (
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.application.historical_replay import CreateReplayRun, CreateReplayRunCommand
from apex.domain.corporate_fundamentals import (
    CorporateDataQualityReport,
    CorporateEvidenceCaptureBounds,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortMember,
    CorporateEvidenceCohortPlan,
    CorporateEvidenceCohortResolution,
    CorporateFilingEvent,
    CorporateHistoryDatasetSnapshot,
    FilingForm,
    FiscalPeriod,
    FundamentalConcept,
    FundamentalFact,
    IssuerCaptureOutcome,
    IssuerCaptureStatus,
    assess_issuer_quality,
)
from apex.domain.corporate_market_research import (
    ALLOWED_HORIZONS_SESSIONS,
    CorporateFeatureId,
    CorporateHypothesisFamily,
    CorporateMarketResearchAgenda,
    CorporateOutcomeId,
    GeneratedCorporateHypothesis,
)
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.cross_market_research import (
    AgendaQualityReport,
    AgendaRun,
    ConditionRule,
    DiscoveryStatus,
    FollowUpKind,
    FollowUpProposal,
    HypothesisExecutionRecord,
    KnowledgeStatus,
    MarketId,
    ProposalSource,
    ResearchAgenda,
    ResearchContradiction,
    ResearchKnowledgeEntry,
)
from apex.domain.genuine_data_quality_gate import GenuineCohortQualityGateReport
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.research_methodology import (
    ResearchConclusion,
    ResearchConclusionState,
    ResearchPhase,
    SampleQualityState,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 9, 15, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seeded_market_lineage(engine: Engine) -> tuple[str, str, str]:
    """The genuine equity/replay/cross-market lineage chain migration
    0089's foreign keys require -- identical construction to
    ``test_postgres_cross_market_research._seeded_lineage``."""

    historical_repository = PostgresHistoricalMarketDataRepository(engine)
    policy = HistoricalCapturePolicy.create(
        provider="ALPACA_MARKET_DATA_V2",
        start_date="2020-07-27",
        end_date="2026-08-06",
        chunk_size=10,
        maximum_instruments=10,
        maximum_requests=10,
        maximum_bytes=1_000_000,
    )
    universe = (
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable_ZZZ", symbol_at_capture="ZZZ", asset_class="COMMON_STOCK"
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="cmr3-test-plan",
        idempotency_key="corporate-market-research-postgres-test-v1",
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(),
        source_operation_ids=("op-1",),
        observation_count=0,
        completeness_state=HistoricalDatasetCompletenessState.PARTIAL,
        earliest_observation_date=None,
        latest_observation_date=None,
        created_at=NOW,
    )
    saved_snapshot = historical_repository.save_snapshot(snapshot)

    replay_repository = PostgresHistoricalReplayRepository(engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=saved_snapshot.snapshot_id,
            start_date=saved_snapshot.start_date,
            end_date=saved_snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="cmr3-test-op",
            idempotency_key="cmr3-test-replay-run",
            created_at=NOW,
        )
    )

    genuine_repository = PostgresGenuineMultiAssetHistoryRepository(engine)
    report = genuine_repository.save_quality_gate_report(
        GenuineCohortQualityGateReport.build(
            snapshot_id=saved_snapshot.snapshot_id,
            cohort_policy_id="cohort-policy-test-v1",
            requested_symbols=("ZZZ",),
            assessments=(),
            point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
            source_hashes=(),
            created_at=NOW,
        )
    )
    dataset = genuine_repository.save_cross_market_dataset(
        CrossMarketDataset.create(
            equity_dataset_snapshot_id=saved_snapshot.snapshot_id,
            equity_dataset_snapshot_hash=saved_snapshot.canonical_hash,
            equity_coverage_start="2020-07-27",
            equity_coverage_end="2026-08-06",
            cohort_policy_id="cohort-policy-test-v1",
            cohort_policy_hash="a" * 64,
            quality_gate_report_id=report.report_id,
            quality_gate_report_hash=report.canonical_hash,
            crypto_provider="COINBASE",
            crypto_product_id="BTC-USD",
            crypto_capture_ids=("btc-a",),
            crypto_coverage_start="2020-07-01",
            crypto_coverage_end="2026-06-30",
            tmc_component_symbols=("SPY", "TLT", "USO"),
            tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
            adjustment_policy_id="ALPACA_IEX_RAW_NO_ADJUSTMENT_V1",
            missing_data_policy_id="FAIL_CLOSED_NO_FORWARD_FILL_V1",
            alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
            point_in_time_limitations=(),
            created_at=NOW,
        )
    )
    return saved_snapshot.snapshot_id, run.replay_run_id, dataset.cross_market_dataset_id


def _seeded_corporate_dataset(engine: Engine) -> str:
    """The genuine corporate-evidence lineage migration 0089's
    ``corporate_dataset_id`` foreign key requires."""

    repository = PostgresCorporateFundamentalsRepository(engine)
    resolution = CorporateEvidenceCohortResolution(
        members=(
            CorporateEvidenceCohortMember(
                "sec-aapl", "AAPL", "320193", "TECHNOLOGY", "COMMON_STOCK"
            ),
        ),
        excluded_missing_cik=("SPY",),
        excluded_unresolved=(),
    )
    plan = repository.save_plan(
        CorporateEvidenceCohortPlan.create(
            cohort_policy_id="genuine_multi_asset_cohort_policy_test",
            cohort_policy_hash="0" * 64,
            resolution=resolution,
            bounds=CorporateEvidenceCaptureBounds.create(),
            created_at=NOW,
        )
    )
    filing = repository.save_filing(
        CorporateFilingEvent.create(
            stable_security_id="sec-aapl",
            cik="320193",
            accession_number="0000320193-24-000123",
            form=FilingForm.FORM_10K,
            report_period="2024-09-28",
            filed_at=datetime(2024, 11, 1, tzinfo=UTC),
            accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
            primary_document="aapl-20240928.htm",
            file_number="001-36743",
            is_xbrl=True,
            is_inline_xbrl=True,
        )
    )
    fact = repository.save_fact(
        FundamentalFact.create(
            stable_security_id="sec-aapl",
            cik="320193",
            concept=FundamentalConcept.REVENUE,
            source_taxonomy="us-gaap",
            source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            fiscal_year=2024,
            fiscal_period=FiscalPeriod.FY,
            period_start="2023-10-01",
            period_end="2024-09-28",
            value=Decimal("391035000000"),
            accession_number="0000320193-24-000123",
            form="10-K",
            filed_at=datetime(2024, 11, 1, tzinfo=UTC),
            accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
        )
    )
    outcome = IssuerCaptureOutcome(
        "sec-aapl",
        "320193",
        IssuerCaptureStatus.CAPTURED,
        1,
        1,
        5000,
        200000,
        2,
        (),
        ("raw-sha-1",),
    )
    checkpoint = repository.save_checkpoint(
        CorporateEvidenceCaptureCheckpoint.create(
            plan=plan,
            run_id="run-1",
            outcomes=(outcome,),
            previously_processed_ciks=(),
            total_requests_this_run=2,
            total_bytes_this_run=205000,
            runtime_seconds=Decimal("3.5"),
            created_at=NOW,
        )
    )
    assessment = assess_issuer_quality(
        stable_security_id="sec-aapl", symbol="AAPL", cik="320193", filings=(filing,), facts=(fact,)
    )
    report = repository.save_quality_report(
        CorporateDataQualityReport.create(
            plan=plan,
            issuer_assessments=(assessment,),
            all_filings=(filing,),
            all_facts=(fact,),
            checkpoint_complete=False,
            created_at=NOW,
        )
    )
    dataset = repository.save_dataset_snapshot(
        CorporateHistoryDatasetSnapshot.create(
            plan=plan,
            checkpoint_ids=(checkpoint.checkpoint_id,),
            filings=(filing,),
            facts=(fact,),
            quality_report=report,
            source_receipt_hashes=("raw-sha-1",),
            created_at=NOW,
        )
    )
    return dataset.dataset_id


def _agenda(
    equity_snapshot_id: str,
    replay_run_id: str,
    cross_market_dataset_id: str,
    corporate_dataset_id: str,
) -> CorporateMarketResearchAgenda:
    return CorporateMarketResearchAgenda.create(
        version="V3",
        objective="Persistence proof agenda.",
        corporate_dataset_id=corporate_dataset_id,
        equity_snapshot_id=equity_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        cohort_policy_id="cohort-policy-test-v1",
        allowed_feature_ids=tuple(CorporateFeatureId),
        allowed_outcome_ids=tuple(CorporateOutcomeId),
        allowed_horizons_sessions=ALLOWED_HORIZONS_SESSIONS,
        allowed_templates=("CROSS_SECTIONAL_V1", "INTERACTION_V1"),
        maximum_genuine_hypothesis_count=12,
        maximum_hypothesis_count=15,
        maximum_configurations_per_hypothesis=3,
        maximum_total_trials=50,
        train_validation_holdout_policy="TRAIN<VALIDATION<HOLDOUT(locked)",
        multiple_testing_family_policy="BH_FDR_PER_FAMILY_V1",
        robustness_requirements=("LEAVE_ONE_PERIOD_OUT",),
        lookback_years=6,
        registered_at=NOW,
    )


def _hypothesis(
    agenda: CorporateMarketResearchAgenda, ordinal: int = 1
) -> GeneratedCorporateHypothesis:
    return GeneratedCorporateHypothesis.create(
        agenda=agenda,
        template_id="CROSS_SECTIONAL_V1",
        template_version="V1",
        family=CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        condition_label=None,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        benchmark_kind="EQUAL_WEIGHT_COHORT",
        rationale="Persistence proof hypothesis.",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=False,
        negative_control_kind=None,
        discovery_status=DiscoveryStatus.PREDECLARED,
        ordinal=ordinal,
        created_at=NOW,
    )


def _real_conclusion(
    engine: Engine,
    hypothesis: GeneratedCorporateHypothesis,
    replay_run_id: str,
    dataset_snapshot_id: str,
    *,
    conclusion_state: ResearchConclusionState = ResearchConclusionState.INSUFFICIENT_EVIDENCE,
    rationale: str = "Persistence proof conclusion.",
) -> ResearchConclusion:
    conclusion = ResearchConclusion.create(
        hypothesis_version_id=hypothesis.hypothesis_id,
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        phase=ResearchPhase.VALIDATION,
        training_trial_id=None,
        validation_trial_id=None,
        holdout_trial_id=None,
        walk_forward_run_id=None,
        walk_forward_fold_count=0,
        robustness_run_id=None,
        trial_family_id=None,
        sample_quality_state=SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE,
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        benchmark_kind=None,
        beats_benchmark=None,
        conclusion_state=conclusion_state,
        rationale=rationale,
        blockers=("INSUFFICIENT_EFFECTIVE_SAMPLE",),
        created_at=NOW,
    )
    return PostgresResearchMethodologyRepository(engine).save_conclusion(conclusion)


def test_corporate_market_research_tables_exist_at_head(postgres_engine: Engine) -> None:
    table_names = set(sa.inspect(postgres_engine).get_table_names())
    assert table_names >= set(CORPORATE_MARKET_RESEARCH_TABLES)


def test_agenda_and_hypothesis_round_trip_and_are_idempotent(postgres_engine: Engine) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    repository = PostgresCorporateMarketResearchRepository(postgres_engine)

    agenda = _agenda(
        equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id
    )
    saved_agenda = repository.save_agenda(agenda)
    repository.save_agenda(agenda)  # idempotent replay
    assert repository.agenda_by_id(agenda.agenda_id) == saved_agenda
    assert agenda in repository.list_agendas()

    hypothesis = _hypothesis(agenda)
    repository.save_hypothesis(hypothesis)
    repository.save_hypothesis(hypothesis)  # idempotent replay
    assert repository.hypothesis_by_id(hypothesis.hypothesis_id) == hypothesis
    assert repository.hypotheses_for_agenda(agenda.agenda_id) == (hypothesis,)


def test_agenda_run_is_the_one_mutable_table_and_updates_in_place(postgres_engine: Engine) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    repository = PostgresCorporateMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    hypothesis = repository.save_hypothesis(_hypothesis(agenda))

    # See application.corporate_market_research's identical, documented cast: AgendaRun.draft
    # is typed to cross_market_research.ResearchAgenda but its body only reads
    # .agenda_id/.maximum_hypothesis_count, both present here with compatible types.
    run = AgendaRun.draft(
        agenda=cast(ResearchAgenda, agenda),
        hypothesis_ids=(hypothesis.hypothesis_id,),
        created_at=NOW,
    )
    repository.save_agenda_run(run)
    advanced = run.advance(
        hypothesis_id=hypothesis.hypothesis_id, trials_added=3, is_final=True, at=NOW
    )
    repository.save_agenda_run(advanced)

    fresh = repository.agenda_run_by_agenda(agenda.agenda_id)
    assert fresh is not None
    assert fresh.agenda_run_id == run.agenda_run_id
    assert fresh.status.value == "COMPLETE"
    assert fresh.trials_executed == 3
    assert repository.agenda_run_by_id(run.agenda_run_id) == fresh


def test_execution_knowledge_contradiction_follow_up_and_quality_report_round_trip(
    postgres_engine: Engine,
) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    repository = PostgresCorporateMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    first = repository.save_hypothesis(_hypothesis(agenda, ordinal=1))
    second = repository.save_hypothesis(_hypothesis(agenda, ordinal=2))

    first_conclusion = _real_conclusion(postgres_engine, first, replay_run_id, equity_snapshot_id)
    second_conclusion = _real_conclusion(
        postgres_engine,
        second,
        replay_run_id,
        equity_snapshot_id,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        rationale="Second, distinct conclusion.",
    )

    from apex.domain.historical_replay import (
        ResearchPeriodDeclaration,
        ResearchPeriodPolicy,
        ResearchPeriodRole,
    )
    from apex.domain.research_methodology import GovernanceIntent, ResearchGovernancePolicy

    period_policy = ResearchPeriodPolicy.create(
        replay_run_id=replay_run_id,
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2020-08-01", "2024-01-01", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2024-01-02", "2025-01-01", False
            ),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2025-01-02", "2026-08-06", True),
        ),
        minimum_sample_count=10,
        created_at=NOW,
    )
    governance_policy = ResearchGovernancePolicy.create(
        hypothesis_version_id=first.hypothesis_id,
        dataset_snapshot_id=equity_snapshot_id,
        replay_run_id=replay_run_id,
        intent=GovernanceIntent.PROMOTION_TRACK,
        period_policy=period_policy,
        revision=1,
        supersedes_governance_policy_id=None,
        declared_at=NOW,
    )
    PostgresResearchMethodologyRepository(postgres_engine).save_governance_policy(governance_policy)

    execution = HypothesisExecutionRecord.create(
        hypothesis_id=first.hypothesis_id,
        agenda_id=agenda.agenda_id,
        governance_policy_id=governance_policy.governance_policy_id,
        exploratory_trial_id=None,
        validation_trial_id=None,
        holdout_trial_id=None,
        walk_forward_trial_ids=(),
        robustness_run_id=None,
        trial_family_id=None,
        conclusion_id=first_conclusion.conclusion_id,
        promotion_evaluation_id=None,
        stability=None,
        evidence_shape="CORPORATE_MARKET_PANEL_V3",
        blockers=(),
        created_at=NOW,
    )
    repository.save_execution(execution)
    repository.save_execution(execution)  # idempotent replay
    assert repository.execution_by_hypothesis(first.hypothesis_id) == execution
    assert repository.executions_for_agenda(agenda.agenda_id) == (execution,)

    first_entry = ResearchKnowledgeEntry.create(
        hypothesis_id=first.hypothesis_id,
        conclusion_id=first_conclusion.conclusion_id,
        status=KnowledgeStatus.ACTIVE_RESEARCH,
        evidence_strength="insufficient sample",
        phase_reached="VALIDATION",
        markets=(MarketId.US_EQUITY,),
        regime_dependence="INSUFFICIENT_EVIDENCE",
        benchmark_kind="NONE",
        holdout_result="UNAVAILABLE",
        robustness_state="NOT_PASSED_OR_UNAVAILABLE",
        cross_sectional_or_time_series="CROSS_SECTIONAL_PANEL",
        limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        revision=1,
        supersedes_entry_id=None,
        created_at=NOW,
    )
    repository.save_knowledge_entry(first_entry)
    repository.save_knowledge_entry(first_entry)  # idempotent replay
    second_entry = ResearchKnowledgeEntry.create(
        hypothesis_id=second.hypothesis_id,
        conclusion_id=second_conclusion.conclusion_id,
        status=KnowledgeStatus.WEAK,
        evidence_strength="weak support",
        phase_reached="VALIDATION",
        markets=(MarketId.US_EQUITY,),
        regime_dependence="STABLE",
        benchmark_kind="EQUAL_WEIGHT_COHORT",
        holdout_result="True",
        robustness_state="PASSED",
        cross_sectional_or_time_series="CROSS_SECTIONAL_PANEL",
        limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        revision=1,
        supersedes_entry_id=None,
        created_at=NOW,
    )
    repository.save_knowledge_entry(second_entry)

    assert repository.knowledge_entries_for_hypothesis(first.hypothesis_id) == (first_entry,)
    assert set(repository.list_knowledge_entries()) == {first_entry, second_entry}

    contradiction = ResearchContradiction.create(
        first_entry_id=first_entry.entry_id,
        second_entry_id=second_entry.entry_id,
        shared_evidence=(CorporateFeatureId.REVENUE_GROWTH_YOY.value,),
        first_holds_when="template=CROSS_SECTIONAL_V1",
        second_holds_when="template=CROSS_SECTIONAL_V1",
        description="Opposite benchmark-relative direction across two retained hypotheses.",
        created_at=NOW,
    )
    repository.save_contradiction(contradiction)
    repository.save_contradiction(contradiction)  # idempotent replay
    assert repository.list_contradictions() == (contradiction,)

    follow_up = FollowUpProposal.create(
        parent_conclusion_id=first_conclusion.conclusion_id,
        parent_hypothesis_id=first.hypothesis_id,
        kind=FollowUpKind.INVESTIGATE_ROBUSTNESS_FAILURE,
        proposed_template_id="CROSS_SECTIONAL_V1",
        description="Bounded follow-up.",
        created_at=NOW,
    )
    repository.save_follow_up(follow_up)
    repository.save_follow_up(follow_up)  # idempotent replay
    assert repository.list_follow_ups() == (follow_up,)

    report = AgendaQualityReport.create(
        agenda_id=agenda.agenda_id,
        dataset_limitations=("SURVIVORSHIP_LIMITED",),
        provider_coverage_limitations=("SIX_YEAR_BOUNDED_CORPORATE_LOOKBACK",),
        panel_dependence_note="Effective sample uses the distinct-date count.",
        multiple_testing_burden_note="One family-level correction applied.",
        negative_control_behavior_note="Two negative controls retained.",
        holdout_integrity_note="Locked holdout read only for evaluation.",
        conclusion_distribution=(("INSUFFICIENT_EVIDENCE", 1), ("WEAK_SUPPORT", 1)),
        hypotheses_proposed=2,
        trials_run=2,
        created_at=NOW,
    )
    repository.save_quality_report(report)
    repository.save_quality_report(report)  # idempotent replay
    assert repository.quality_report_by_agenda(agenda.agenda_id) == report


def test_corporate_market_research_immutable_rows_reject_update_and_delete(
    postgres_engine: Engine,
) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    repository = PostgresCorporateMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )

    with postgres_engine.begin() as connection, pytest.raises(sa.exc.DBAPIError):
        connection.execute(
            sa.text(
                "UPDATE corporate_market_research_agenda SET version = 'X' WHERE agenda_id = :id"
            ),
            {"id": agenda.agenda_id},
        )
    with postgres_engine.begin() as connection, pytest.raises(sa.exc.DBAPIError):
        connection.execute(
            sa.text("DELETE FROM corporate_market_research_agenda WHERE agenda_id = :id"),
            {"id": agenda.agenda_id},
        )


def test_agenda_run_is_not_protected_by_the_immutable_trigger(postgres_engine: Engine) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    repository = PostgresCorporateMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    hypothesis = repository.save_hypothesis(_hypothesis(agenda))
    # See application.corporate_market_research's identical, documented cast: AgendaRun.draft
    # is typed to cross_market_research.ResearchAgenda but its body only reads
    # .agenda_id/.maximum_hypothesis_count, both present here with compatible types.
    run = AgendaRun.draft(
        agenda=cast(ResearchAgenda, agenda),
        hypothesis_ids=(hypothesis.hypothesis_id,),
        created_at=NOW,
    )
    repository.save_agenda_run(run)
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE corporate_market_agenda_run SET cursor_ordinal = 99 "
                "WHERE agenda_run_id = :id"
            ),
            {"id": run.agenda_run_id},
        )
    # The direct UPDATE above succeeding (no immutable-trigger exception) is this test's
    # entire proof; the discrete cursor_ordinal column is a query convenience only -- the
    # repository's own rehydration reads the separate payload column, which
    # save_agenda_run (not a raw UPDATE) is responsible for keeping in sync.
    with postgres_engine.connect() as connection:
        raw_cursor = connection.execute(
            sa.text(
                "SELECT cursor_ordinal FROM corporate_market_agenda_run WHERE agenda_run_id = :id"
            ),
            {"id": run.agenda_run_id},
        ).scalar_one()
    assert raw_cursor == 99


def test_retained_corporate_market_research_evidence_blocks_destructive_0089_downgrade(
    postgres_engine: Engine,
) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    PostgresCorporateMarketResearchRepository(postgres_engine).save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    with (
        postgres_engine.connect() as connection,
        pytest.raises(Exception, match="cannot downgrade 0089"),
    ):
        command.downgrade(_config(connection), "0088_corporate_fundamentals")


def test_corporate_market_research_never_touches_crypto_kalshi_or_options_tables(
    postgres_engine: Engine,
) -> None:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(
        postgres_engine
    )
    corporate_dataset_id = _seeded_corporate_dataset(postgres_engine)
    unrelated_tables = (
        "apex_crypto_paper_account",
        "apex_kalshi_paper_account",
        "options_chain_snapshot",
    )
    existing_tables = set(sa.inspect(postgres_engine).get_table_names())
    before = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    PostgresCorporateMarketResearchRepository(postgres_engine).save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    after = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    assert before == after


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one())
