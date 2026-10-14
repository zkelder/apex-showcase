"""PostgreSQL persistence proofs for migration 0090: the corporate
calibration-depth plan, its one bounded mutable run cursor, null-control
provenance and outcome evidence, non-destructive reassessment records, v3
follow-up qualification, and the retained quality report -- round-trip,
idempotent replay, immutable-evidence enforcement, mutable-cursor updates,
and downgrade protection. Every governed trial/robustness/conclusion row a
null-control outcome references is persisted through the exact same,
unmodified migration-0079 tables the v3 agenda already reuses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.corporate_calibration_depth_postgres import (
    CORPORATE_CALIBRATION_DEPTH_TABLES,
    PostgresCorporateCalibrationDepthRepository,
)
from apex.adapters.corporate_fundamentals_postgres import (
    PostgresCorporateFundamentalsRepository,
)
from apex.adapters.corporate_market_research_postgres import (
    PostgresCorporateMarketResearchRepository,
)
from apex.adapters.genuine_multi_asset_history_postgres import (
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.application.historical_replay import CreateReplayRun, CreateReplayRunCommand
from apex.domain.corporate_calibration_depth import (
    CorporateCalibrationPlan,
    CorporateCalibrationReport,
    CorporateCalibrationRun,
    CorporateNullControlOutcome,
    CorporateNullControlSpec,
    CorporateNullMechanism,
    CorporateReassessment,
    CorporateReassessmentSubjectKind,
    CorporateStratumTarget,
    corporate_calibration_depth_warnings,
)
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
    ConditionRule,
    DiscoveryStatus,
    FollowUpKind,
    FollowUpProposal,
    ProposalSource,
)
from apex.domain.genuine_data_quality_gate import GenuineCohortQualityGateReport
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.research_calibration import EvidenceGrade
from apex.domain.research_calibration_v2 import (
    ConditioningShape,
    FollowUpQualification,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    ResearchDesign,
    classify_follow_up_v2,
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
STRATUM = "CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|NUMERIC_SPLIT"


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seeded_market_lineage(engine: Engine) -> tuple[str, str, str]:
    """Identical construction to ``test_postgres_corporate_market_
    research._seeded_market_lineage`` -- the genuine equity/replay/cross-
    market lineage chain migrations 0089/0090 both foreign-key against."""

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
        operator_action_id="ccd-test-plan",
        idempotency_key="corporate-calibration-depth-postgres-test-v1",
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
            operator_action_id="ccd-test-op",
            idempotency_key="ccd-test-replay-run",
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
    """Identical construction to ``test_postgres_corporate_market_
    research._seeded_corporate_dataset``."""

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
        objective="Calibration-depth persistence proof agenda.",
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
    agenda: CorporateMarketResearchAgenda, *, ordinal: int = 1, is_negative_control: bool = False
) -> GeneratedCorporateHypothesis:
    return GeneratedCorporateHypothesis.create(
        agenda=agenda,
        template_id="CROSS_SECTIONAL_V1",
        template_version="V1",
        family=(
            CorporateHypothesisFamily.NEGATIVE_CONTROL
            if is_negative_control
            else CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL
        ),
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        condition_label=None,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        benchmark_kind="EQUAL_WEIGHT_COHORT",
        rationale="Calibration-depth persistence proof hypothesis.",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=is_negative_control,
        negative_control_kind=("RANDOMIZED_ISSUER_ASSIGNMENT" if is_negative_control else None),
        discovery_status=(
            DiscoveryStatus.NEGATIVE_CONTROL if is_negative_control else DiscoveryStatus.PREDECLARED
        ),
        ordinal=ordinal,
        created_at=NOW,
    )


def _real_conclusion(
    engine: Engine,
    hypothesis: GeneratedCorporateHypothesis,
    replay_run_id: str,
    dataset_snapshot_id: str,
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
        conclusion_state=ResearchConclusionState.INSUFFICIENT_EVIDENCE,
        rationale="Calibration-depth persistence proof conclusion.",
        blockers=("INSUFFICIENT_EFFECTIVE_SAMPLE",),
        created_at=NOW,
    )
    return PostgresResearchMethodologyRepository(engine).save_conclusion(conclusion)


def _seeded_calibration_lineage(
    engine: Engine,
) -> tuple[
    CorporateMarketResearchAgenda,
    GeneratedCorporateHypothesis,
    GeneratedCorporateHypothesis,
    str,
    str,
    str,
]:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(engine)
    corporate_dataset_id = _seeded_corporate_dataset(engine)
    market_repository = PostgresCorporateMarketResearchRepository(engine)
    agenda = market_repository.save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    genuine = market_repository.save_hypothesis(_hypothesis(agenda, ordinal=1))
    control = market_repository.save_hypothesis(
        _hypothesis(agenda, ordinal=2, is_negative_control=True)
    )
    return (
        agenda,
        genuine,
        control,
        corporate_dataset_id,
        equity_snapshot_id,
        replay_run_id,
    )


def _plan(
    agenda: CorporateMarketResearchAgenda,
    genuine: GeneratedCorporateHypothesis,
    *,
    corporate_dataset_id: str,
    equity_snapshot_id: str,
    replay_run_id: str,
) -> CorporateCalibrationPlan:
    target = CorporateStratumTarget.create(
        matched_stratum=STRATUM,
        research_design=ResearchDesign.CROSS_SECTIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        genuine_hypothesis_ids=(genuine.hypothesis_id,),
        backing_feature_ids=(CorporateFeatureId.REVENUE_GROWTH_YOY,),
        original_matched_null_count=0,
        target_new_control_count=24,
    )
    return CorporateCalibrationPlan.create(
        version="CORPORATE_CALIBRATION_DEPTH_V1",
        objective="Calibration-depth persistence proof plan.",
        source_agenda_id=agenda.agenda_id,
        corporate_dataset_id=corporate_dataset_id,
        equity_snapshot_id=equity_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=agenda.cross_market_dataset_id,
        stratum_targets=(target,),
        controls_per_stratum=24,
        random_seed_policy="Fixed per-mechanism seed bases.",
        registered_at=NOW,
    )


def _spec(plan: CorporateCalibrationPlan, *, ordinal: int = 1) -> CorporateNullControlSpec:
    return CorporateNullControlSpec.create(
        calibration_id=plan.calibration_id,
        matched_stratum=STRATUM,
        research_design=ResearchDesign.CROSS_SECTIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        template_id="CROSS_SECTIONAL_V1",
        mechanism=CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        seed=20260809402,
        preserved_properties=("MARKET_DATE_STRUCTURE",),
        destroyed_relationship="ISSUER_SPECIFIC_FEATURE_OUTCOME_PAIRING",
        rationale="Calibration-depth persistence proof control.",
        ordinal=ordinal,
        created_at=NOW,
    )


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()


# ==========================================================================
# Tables exist; plan/spec/outcome/reassessment/qualification/report round trip
# ==========================================================================


def test_corporate_calibration_depth_tables_exist_at_head(postgres_engine: Engine) -> None:
    table_names = set(sa.inspect(postgres_engine).get_table_names())
    assert table_names >= set(CORPORATE_CALIBRATION_DEPTH_TABLES)


def test_plan_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = _plan(
        agenda,
        genuine,
        corporate_dataset_id=corp_id,
        equity_snapshot_id=equity_id,
        replay_run_id=replay_id,
    )
    saved = repository.save_plan(plan)
    repository.save_plan(plan)  # idempotent replay
    assert repository.plan_by_id(plan.calibration_id) == saved
    assert plan in repository.list_plans()


def test_run_is_the_one_mutable_table_and_updates_in_place(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    spec = repository.save_spec(_spec(plan))
    run = CorporateCalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    repository.save_run(run)
    advanced = run.advance(spec_id=spec.spec_id, trials_added=3, is_final=True, at=NOW)
    repository.save_run(advanced)

    fresh = repository.run_by_calibration(plan.calibration_id)
    assert fresh is not None
    assert fresh.calibration_run_id == run.calibration_run_id
    assert fresh.status.value == "COMPLETE"
    assert fresh.trials_executed == 3
    assert repository.run_by_id(run.calibration_run_id) == fresh


def test_spec_and_outcome_round_trip_and_are_idempotent(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    spec = repository.save_spec(_spec(plan))
    repository.save_spec(_spec(plan))  # idempotent replay (same ordinal -> identical spec_id)
    assert repository.spec_by_id(spec.spec_id) == spec
    assert repository.specs_for_calibration(plan.calibration_id) == (spec,)
    assert repository.specs_for_stratum(STRATUM) == (spec,)

    run = repository.save_run(
        CorporateCalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    )
    conclusion = _real_conclusion(postgres_engine, genuine, replay_id, equity_id)
    outcome = CorporateNullControlOutcome.create(
        spec_id=spec.spec_id,
        calibration_run_id=run.calibration_run_id,
        governance_policy_id="governance-policy-1",
        exploratory_trial_id=None,
        validation_trial_id=None,
        holdout_trial_id=None,
        robustness_run_id=None,
        conclusion_id=conclusion.conclusion_id,
        promotion_evaluation_id=None,
        knowledge_status="ACTIVE_RESEARCH",
        cohens_d=Decimal("0.0312"),
        blockers=("VALIDATION_SAMPLE_INSUFFICIENT",),
        created_at=NOW,
    )
    repository.save_outcome(outcome)
    repository.save_outcome(outcome)  # idempotent replay
    assert repository.outcome_by_spec(spec.spec_id) == outcome
    assert repository.outcomes_for_run(run.calibration_run_id) == (outcome,)


def test_reassessment_round_trips_for_both_subject_kinds(postgres_engine: Engine) -> None:
    agenda, genuine, control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    genuine_conclusion = _real_conclusion(postgres_engine, genuine, replay_id, equity_id)
    control_conclusion = _real_conclusion(postgres_engine, control, replay_id, equity_id)

    genuine_reassessment = CorporateReassessment.create(
        calibration_id=plan.calibration_id,
        subject_id=genuine.hypothesis_id,
        subject_kind=CorporateReassessmentSubjectKind.HYPOTHESIS,
        matched_stratum=STRATUM,
        original_conclusion_id=genuine_conclusion.conclusion_id,
        original_conclusion_state="INSUFFICIENT_EVIDENCE",
        original_evidence_grade="EXPLORATORY",
        original_matched_null_count=0,
        new_matched_null_count=24,
        new_matched_percentile=Decimal("0.42"),
        new_pooled_null_count=240,
        new_evidence_grade=EvidenceGrade.EXPLORATORY,
        cohens_d=Decimal("0.05"),
        rationale="Persistence proof genuine reassessment.",
        created_at=NOW,
    )
    control_reassessment = CorporateReassessment.create(
        calibration_id=plan.calibration_id,
        subject_id=control.hypothesis_id,
        subject_kind=CorporateReassessmentSubjectKind.NEGATIVE_CONTROL,
        matched_stratum=STRATUM,
        original_conclusion_id=control_conclusion.conclusion_id,
        original_conclusion_state="INSUFFICIENT_EVIDENCE",
        original_evidence_grade="NOT_PREVIOUSLY_GRADED_AS_CALIBRATION_SUBJECT",
        original_matched_null_count=0,
        new_matched_null_count=24,
        new_matched_percentile=Decimal("0.11"),
        new_pooled_null_count=240,
        new_evidence_grade=EvidenceGrade.UNRELIABLE,
        cohens_d=Decimal("0.01"),
        rationale="Persistence proof control reassessment.",
        created_at=NOW,
    )
    repository.save_reassessment(genuine_reassessment)
    repository.save_reassessment(genuine_reassessment)  # idempotent replay
    repository.save_reassessment(control_reassessment)

    retained = repository.reassessments_for_calibration(plan.calibration_id)
    assert set(retained) == {genuine_reassessment, control_reassessment}
    assert (
        repository.reassessment_for_subject(
            calibration_id=plan.calibration_id, subject_id=genuine.hypothesis_id
        )
        == genuine_reassessment
    )


def test_follow_up_qualification_round_trips(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    market_repository = PostgresCorporateMarketResearchRepository(postgres_engine)
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    conclusion = _real_conclusion(postgres_engine, genuine, replay_id, equity_id)
    follow_up = FollowUpProposal.create(
        parent_conclusion_id=conclusion.conclusion_id,
        parent_hypothesis_id=genuine.hypothesis_id,
        kind=FollowUpKind.INVESTIGATE_ROBUSTNESS_FAILURE,
        proposed_template_id="CROSS_SECTIONAL_V1",
        description="Bounded follow-up.",
        created_at=NOW,
    )
    market_repository.save_follow_up(follow_up)

    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("0.31"),
        matched_null_statistics=tuple(Decimal(str(i)) * Decimal("0.01") for i in range(24)),
        pooled_null_statistics=tuple(Decimal(str(i)) * Decimal("0.01") for i in range(240)),
        matched_stratum=STRATUM,
    )
    qualification = FollowUpQualification.create(
        calibration_id=plan.calibration_id,
        follow_up_id=follow_up.follow_up_id,
        parent_hypothesis_id=genuine.hypothesis_id,
        research_design=ResearchDesign.CROSS_SECTIONAL,
        horizon_sessions=20,
        matched=matched,
        evidence_grade_v2=EvidenceGrade.EXPLORATORY,
        qualification=classify_follow_up_v2(
            matched=matched, evidence_grade=EvidenceGrade.EXPLORATORY
        ),
        rationale="Persistence proof follow-up qualification.",
        created_at=NOW,
    )
    repository.save_follow_up_qualification(qualification)
    repository.save_follow_up_qualification(qualification)  # idempotent replay
    assert repository.follow_up_qualifications_for_calibration(plan.calibration_id) == (
        qualification,
    )
    # sufficient (24 >= 12) but only EXPLORATORY -- classify_follow_up_v2's own documented rule.
    assert qualification.qualification is FollowUpQualificationV2.NO_LONGER_JUSTIFIED


def test_report_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    spec = repository.save_spec(_spec(plan))
    run = repository.save_run(
        CorporateCalibrationRun.draft(
            calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW
        ).advance(spec_id=spec.spec_id, trials_added=1, is_final=True, at=NOW)
    )
    report = CorporateCalibrationReport.create(
        calibration_id=plan.calibration_id,
        calibration_run_id=run.calibration_run_id,
        strata_evaluated=1,
        controls_generated=24,
        controls_per_stratum=24,
        raw_false_positive_rate=Decimal("0.01"),
        corrected_false_positive_rate=Decimal("0.005"),
        false_weak_rate=Decimal("0.2"),
        false_suggestive_rate=Decimal("0.1"),
        false_validated_rate=Decimal("0"),
        false_promotion_rate=Decimal("0"),
        design_breakdown_labels=("CROSS_SECTIONAL",),
        stability_slice_labels=("TIME_PERIOD:EARLY",),
        sector_concentration_note="note",
        filing_event_sparsity_note="note",
        weak_support_diagnostic_note="note",
        genuine_reassessment_count=1,
        genuine_assessment_changed_count=1,
        control_reassessment_count=1,
        control_now_validated_or_strong_count=0,
        incremental_calibration_count=0,
        policy_v2_justified=False,
        policy_v2_id=None,
        policy_recommendation_rationale="rationale",
        limitations=corporate_calibration_depth_warnings(),
        created_at=NOW,
    )
    repository.save_report(report)
    repository.save_report(report)  # idempotent replay
    assert repository.report_by_calibration(plan.calibration_id) == report


# ==========================================================================
# Immutability, downgrade protection, and no cross-domain drift
# ==========================================================================


def test_immutable_tables_reject_update_and_delete(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    with postgres_engine.begin() as connection, pytest.raises(sa.exc.DBAPIError):
        connection.execute(
            sa.text(
                "UPDATE corporate_calibration_plan SET version = 'X' WHERE calibration_id = :id"
            ),
            {"id": plan.calibration_id},
        )
    with postgres_engine.begin() as connection, pytest.raises(sa.exc.DBAPIError):
        connection.execute(
            sa.text("DELETE FROM corporate_calibration_plan WHERE calibration_id = :id"),
            {"id": plan.calibration_id},
        )


def test_run_table_is_not_protected_by_the_immutable_trigger(postgres_engine: Engine) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    repository = PostgresCorporateCalibrationDepthRepository(postgres_engine)
    plan = repository.save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    spec = repository.save_spec(_spec(plan))
    run = repository.save_run(
        CorporateCalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE corporate_calibration_run SET cursor_ordinal = 99 "
                "WHERE calibration_run_id = :id"
            ),
            {"id": run.calibration_run_id},
        )
    with postgres_engine.connect() as connection:
        raw_cursor = connection.execute(
            sa.text(
                "SELECT cursor_ordinal FROM corporate_calibration_run "
                "WHERE calibration_run_id = :id"
            ),
            {"id": run.calibration_run_id},
        ).scalar_one()
    assert raw_cursor == 99


def test_retained_calibration_depth_evidence_blocks_destructive_0090_downgrade(
    postgres_engine: Engine,
) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
    PostgresCorporateCalibrationDepthRepository(postgres_engine).save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    with (
        postgres_engine.connect() as connection,
        pytest.raises(Exception, match="cannot downgrade 0090"),
    ):
        command.downgrade(_config(connection), "0089_corporate_market_research")


def test_corporate_calibration_depth_never_touches_crypto_kalshi_or_options_tables(
    postgres_engine: Engine,
) -> None:
    agenda, genuine, _control, corp_id, equity_id, replay_id = _seeded_calibration_lineage(
        postgres_engine
    )
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
    PostgresCorporateCalibrationDepthRepository(postgres_engine).save_plan(
        _plan(
            agenda,
            genuine,
            corporate_dataset_id=corp_id,
            equity_snapshot_id=equity_id,
            replay_run_id=replay_id,
        )
    )
    after = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    assert before == after
