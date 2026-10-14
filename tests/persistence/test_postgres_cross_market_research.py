"""PostgreSQL persistence proofs for migration 0081: the research
agenda, generated hypotheses, the one bounded mutable agenda-run cursor,
hypothesis-execution evidence, the append-only knowledge base,
contradictions, follow-up proposals, and the agenda quality report --
round-trip, idempotent replay, immutable-evidence enforcement, mutable-
cursor updates, and downgrade protection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.cross_market_research_postgres import (
    CROSS_MARKET_RESEARCH_TABLES,
    PostgresCrossMarketResearchRepository,
)
from apex.adapters.genuine_multi_asset_history_postgres import (
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.application.historical_replay import CreateReplayRun, CreateReplayRunCommand
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.cross_market_research import (
    AgendaQualityReport,
    AgendaRun,
    ConditionRule,
    DiscoveryStatus,
    FeatureFamily,
    FeatureId,
    FollowUpKind,
    FollowUpProposal,
    GeneratedHypothesis,
    HypothesisFamily,
    HypothesisGenerationMethod,
    HypothesisTemplateId,
    KnowledgeStatus,
    MarketId,
    OutcomeFamily,
    OutcomeId,
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
NOW = datetime(2026, 8, 7, 15, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seeded_lineage(engine: Engine) -> tuple[HistoricalDatasetSnapshot, str, str]:
    """Builds the minimum genuine lineage chain migration 0081's foreign
    keys require: one historical dataset snapshot, one replay run bound
    to it, and one cross-market dataset bound to it."""

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
        operator_action_id="cmr-test-plan",
        idempotency_key="cross-market-research-postgres-test-v1",
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
            operator_action_id="cmr-test-op",
            idempotency_key="cmr-test-replay-run",
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
    return saved_snapshot, run.replay_run_id, dataset.cross_market_dataset_id


def _agenda(snapshot_id: str, replay_run_id: str, cross_market_dataset_id: str) -> ResearchAgenda:
    return ResearchAgenda.create(
        version="V1",
        objective="Persistence proof agenda.",
        allowed_markets=tuple(MarketId),
        allowed_feature_families=tuple(FeatureFamily),
        allowed_outcome_families=tuple(OutcomeFamily),
        allowed_horizons_sessions=(5, 20),
        allowed_templates=tuple(HypothesisTemplateId),
        hypothesis_generation_method=HypothesisGenerationMethod.DETERMINISTIC_TEMPLATE_EXPANSION_V1,
        maximum_hypothesis_count=5,
        maximum_configurations_per_hypothesis=1,
        maximum_total_trials=50,
        train_validation_holdout_policy="TRAIN<VALIDATION<HOLDOUT(locked)",
        multiple_testing_family_policy="PARTITIONED_BY_HYPOTHESIS_FAMILY_V1",
        robustness_requirements=("SUBPERIOD_SPLIT",),
        dataset_snapshot_id=snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        cohort_policy_id="cohort-policy-test-v1",
        registered_at=NOW,
    )


def _hypothesis(agenda: ResearchAgenda, ordinal: int = 1) -> GeneratedHypothesis:
    return GeneratedHypothesis.create(
        agenda=agenda,
        template_id=HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        template_version="V1",
        feature_id=FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        condition_label=None,
        secondary_feature_id=None,
        secondary_condition_rule=None,
        secondary_condition_label=None,
        market_scope=(MarketId.US_EQUITY,),
        outcome_id=OutcomeId.EQUITY_FORWARD_RETURN,
        horizon_sessions=20,
        benchmark_kind="UNCONDITIONAL_SUBSEQUENT_RETURN",
        family=HypothesisFamily.EQUITY_INTERNAL,
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
    hypothesis: GeneratedHypothesis,
    replay_run_id: str,
    dataset_snapshot_id: str,
    *,
    conclusion_state: ResearchConclusionState = ResearchConclusionState.INSUFFICIENT_EVIDENCE,
    rationale: str = "Persistence proof conclusion.",
) -> ResearchConclusion:
    """``cross_market_knowledge_entry.conclusion_id`` is a real foreign
    key into ``research_conclusion`` -- this saves one genuine (if
    minimal) conclusion row so knowledge-entry tests exercise the actual
    constraint rather than a fabricated id. ``rationale`` varies the
    identity so two calls for the same hypothesis (e.g. an initial and a
    multiple-testing-corrected revision) yield two genuinely distinct
    conclusion rows."""

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


def test_cross_market_research_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(CROSS_MARKET_RESEARCH_TABLES)


def test_agenda_round_trips_and_save_is_idempotent(postgres_engine: Engine) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = _agenda(snapshot.snapshot_id, replay_run_id, dataset_id)
    first = repository.save_agenda(agenda)
    second = repository.save_agenda(agenda)
    assert first.agenda_id == second.agenda_id == agenda.agenda_id
    fresh = PostgresCrossMarketResearchRepository(postgres_engine)
    loaded = fresh.agenda_by_id(agenda.agenda_id)
    assert loaded == agenda
    assert loaded in fresh.list_agendas()


def test_hypothesis_round_trips_and_lists_by_agenda_in_ordinal_order(
    postgres_engine: Engine,
) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    second = repository.save_hypothesis(_hypothesis(agenda, ordinal=2))
    first = repository.save_hypothesis(_hypothesis(agenda, ordinal=1))
    fresh = PostgresCrossMarketResearchRepository(postgres_engine)
    listed = fresh.hypotheses_for_agenda(agenda.agenda_id)
    assert [item.hypothesis_id for item in listed] == [first.hypothesis_id, second.hypothesis_id]
    assert fresh.hypothesis_by_id(first.hypothesis_id) == first


def test_agenda_run_is_the_one_mutable_table_and_updates_in_place(postgres_engine: Engine) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    hypothesis = repository.save_hypothesis(_hypothesis(agenda))

    run = AgendaRun.draft(agenda=agenda, hypothesis_ids=(hypothesis.hypothesis_id,), created_at=NOW)
    repository.save_agenda_run(run)
    advanced = run.advance(
        hypothesis_id=hypothesis.hypothesis_id, trials_added=4, is_final=True, at=NOW
    )
    repository.save_agenda_run(advanced)

    fresh = PostgresCrossMarketResearchRepository(postgres_engine)
    reloaded = fresh.agenda_run_by_agenda(agenda.agenda_id)
    assert reloaded == advanced
    assert reloaded.agenda_run_id == run.agenda_run_id
    # exactly one row for this agenda -- advancing updated it in place
    with postgres_engine.connect() as connection:
        count = connection.execute(
            sa.text("SELECT count(*) FROM cross_market_agenda_run WHERE agenda_id = :agenda_id"),
            {"agenda_id": agenda.agenda_id},
        ).scalar_one()
    assert count == 1


def test_knowledge_entry_revisions_round_trip_in_order(postgres_engine: Engine) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    hypothesis = repository.save_hypothesis(_hypothesis(agenda))
    conclusion_one = _real_conclusion(
        postgres_engine,
        hypothesis,
        replay_run_id,
        snapshot.snapshot_id,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        rationale="Initial revision.",
    )
    conclusion_two = _real_conclusion(
        postgres_engine,
        hypothesis,
        replay_run_id,
        snapshot.snapshot_id,
        conclusion_state=ResearchConclusionState.REJECTED,
        rationale="Multiple-testing-corrected revision.",
    )

    revision_one = ResearchKnowledgeEntry.create(
        hypothesis_id=hypothesis.hypothesis_id,
        conclusion_id=conclusion_one.conclusion_id,
        status=KnowledgeStatus.WEAK,
        evidence_strength="INITIAL_WEAK_SUPPORT",
        phase_reached="VALIDATION",
        markets=(MarketId.US_EQUITY,),
        regime_dependence="STABLE",
        benchmark_kind="UNCONDITIONAL_SUBSEQUENT_RETURN",
        holdout_result="NOT_EVALUATED",
        robustness_state="NOT_RUN",
        cross_sectional_or_time_series="TIME_SERIES_PANEL_WITH_SHARED_DATES",
        limitations=(),
        revision=1,
        supersedes_entry_id=None,
        created_at=NOW,
    )
    repository.save_knowledge_entry(revision_one)
    revision_two = ResearchKnowledgeEntry.create(
        hypothesis_id=hypothesis.hypothesis_id,
        conclusion_id=conclusion_two.conclusion_id,
        status=KnowledgeStatus.REJECTED,
        evidence_strength="MULTIPLE_TESTING_CORRECTED_REJECTED",
        phase_reached="LOCKED_HOLDOUT",
        markets=(MarketId.US_EQUITY,),
        regime_dependence="STABLE",
        benchmark_kind="UNCONDITIONAL_SUBSEQUENT_RETURN",
        holdout_result="DOES_NOT_BEAT_BENCHMARK",
        robustness_state="PASSED",
        cross_sectional_or_time_series="TIME_SERIES_PANEL_WITH_SHARED_DATES",
        limitations=(),
        revision=2,
        supersedes_entry_id=revision_one.entry_id,
        created_at=NOW,
    )
    repository.save_knowledge_entry(revision_two)

    fresh = PostgresCrossMarketResearchRepository(postgres_engine)
    history = fresh.knowledge_entries_for_hypothesis(hypothesis.hypothesis_id)
    assert [item.revision for item in history] == [1, 2]
    assert history[1].supersedes_entry_id == history[0].entry_id
    assert revision_one in fresh.list_knowledge_entries()
    assert revision_two in fresh.list_knowledge_entries()


def test_cross_market_research_immutable_rows_reject_update_and_delete(
    postgres_engine: Engine,
) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    repository.save_hypothesis(_hypothesis(agenda))

    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE cross_market_research_agenda SET version = 'HACKED' "
                    "WHERE agenda_id = :agenda_id"
                ),
                {"agenda_id": agenda.agenda_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM cross_market_research_agenda"))
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM cross_market_hypothesis"))
        connection.rollback()


def test_agenda_run_is_not_protected_by_the_immutable_trigger(postgres_engine: Engine) -> None:
    """The one deliberate exception: direct UPDATE on the mutable cursor
    table must succeed at the database level (the repository's own
    ``save_agenda_run`` is the only thing that should constrain it)."""

    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    hypothesis = repository.save_hypothesis(_hypothesis(agenda))
    run = AgendaRun.draft(agenda=agenda, hypothesis_ids=(hypothesis.hypothesis_id,), created_at=NOW)
    repository.save_agenda_run(run)
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE cross_market_agenda_run SET cursor_ordinal = 1 "
                "WHERE agenda_run_id = :run_id"
            ),
            {"run_id": run.agenda_run_id},
        )


def test_contradiction_and_follow_up_and_quality_report_round_trip(postgres_engine: Engine) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    first_hypothesis = repository.save_hypothesis(_hypothesis(agenda, ordinal=1))
    second_hypothesis = repository.save_hypothesis(_hypothesis(agenda, ordinal=2))
    first_conclusion = _real_conclusion(
        postgres_engine,
        first_hypothesis,
        replay_run_id,
        snapshot.snapshot_id,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        rationale="First contradiction side.",
    )
    second_conclusion = _real_conclusion(
        postgres_engine,
        second_hypothesis,
        replay_run_id,
        snapshot.snapshot_id,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        rationale="Second contradiction side.",
    )
    first_entry = repository.save_knowledge_entry(
        ResearchKnowledgeEntry.create(
            hypothesis_id=first_hypothesis.hypothesis_id,
            conclusion_id=first_conclusion.conclusion_id,
            status=KnowledgeStatus.WEAK,
            evidence_strength="x",
            phase_reached="VALIDATION",
            markets=(MarketId.US_EQUITY,),
            regime_dependence="STABLE",
            benchmark_kind="UNCONDITIONAL_SUBSEQUENT_RETURN",
            holdout_result="NOT_EVALUATED",
            robustness_state="NOT_RUN",
            cross_sectional_or_time_series="TIME_SERIES_PANEL_WITH_SHARED_DATES",
            limitations=(),
            revision=1,
            supersedes_entry_id=None,
            created_at=NOW,
        )
    )
    second_entry = repository.save_knowledge_entry(
        ResearchKnowledgeEntry.create(
            hypothesis_id=second_hypothesis.hypothesis_id,
            conclusion_id=second_conclusion.conclusion_id,
            status=KnowledgeStatus.WEAK,
            evidence_strength="x",
            phase_reached="VALIDATION",
            markets=(MarketId.BTC,),
            regime_dependence="STABLE",
            benchmark_kind="UNCONDITIONAL_SUBSEQUENT_RETURN",
            holdout_result="NOT_EVALUATED",
            robustness_state="NOT_RUN",
            cross_sectional_or_time_series="TIME_SERIES_PANEL_WITH_SHARED_DATES",
            limitations=(),
            revision=1,
            supersedes_entry_id=None,
            created_at=NOW,
        )
    )
    contradiction = repository.save_contradiction(
        ResearchContradiction.create(
            first_entry_id=first_entry.entry_id,
            second_entry_id=second_entry.entry_id,
            shared_evidence=("BTC_FORWARD_RETURN",),
            first_holds_when="RISK_ON",
            second_holds_when="RISK_OFF",
            description="Conflicting sign across regimes.",
            created_at=NOW,
        )
    )
    follow_up = repository.save_follow_up(
        FollowUpProposal.create(
            parent_conclusion_id=first_conclusion.conclusion_id,
            parent_hypothesis_id=first_hypothesis.hypothesis_id,
            kind=FollowUpKind.SPLIT_BY_REGIME,
            proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
            description="Split by regime.",
            created_at=NOW,
        )
    )
    report = repository.save_quality_report(
        AgendaQualityReport.create(
            agenda_id=agenda.agenda_id,
            dataset_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
            provider_coverage_limitations=(),
            panel_dependence_note="Rows share market dates and are not independent samples.",
            multiple_testing_burden_note="2 hypotheses, 1 family.",
            negative_control_behavior_note="0 negative controls in this proof.",
            holdout_integrity_note="Holdout locked before any holdout trial.",
            conclusion_distribution=(("WEAK", 2),),
            hypotheses_proposed=2,
            trials_run=4,
            created_at=NOW,
        )
    )

    fresh = PostgresCrossMarketResearchRepository(postgres_engine)
    assert contradiction in fresh.list_contradictions()
    assert follow_up in fresh.list_follow_ups()
    assert fresh.quality_report_by_agenda(agenda.agenda_id) == report
    # idempotent re-save of the quality report (unique per agenda)
    assert repository.save_quality_report(report).report_id == report.report_id


def test_retained_cross_market_research_evidence_blocks_destructive_0081_downgrade(
    postgres_engine: Engine,
) -> None:
    snapshot, replay_run_id, dataset_id = _seeded_lineage(postgres_engine)
    repository = PostgresCrossMarketResearchRepository(postgres_engine)
    repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0081 with retained controlled cross-market research evidence",
        ):
            command.downgrade(_config(connection), "0080_genuine_multi_asset_history")
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0081_cross_market_research"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0081_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(CROSS_MARKET_RESEARCH_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0080_genuine_multi_asset_history")
    assert set(CROSS_MARKET_RESEARCH_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(CROSS_MARKET_RESEARCH_TABLES)
