"""PostgreSQL persistence proofs for migration 0091: the corporate regime
follow-up's immutable specification, matched-null-control provenance, and
result -- round-trip, idempotent replay, immutable-evidence enforcement,
and downgrade protection. Every parent-lineage id (follow-up proposal,
hypothesis, conclusion, agenda) is a real foreign key into the exact same,
unmodified 0079/0089/0090 tables Corporate Research Calibration Depth v1
already reuses.
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

from apex.adapters.corporate_fundamentals_postgres import (
    PostgresCorporateFundamentalsRepository,
)
from apex.adapters.corporate_market_research_postgres import (
    PostgresCorporateMarketResearchRepository,
)
from apex.adapters.corporate_regime_followup_postgres import (
    CORPORATE_REGIME_FOLLOWUP_TABLES,
    PostgresCorporateRegimeFollowUpRepository,
)
from apex.adapters.genuine_multi_asset_history_postgres import (
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.application.historical_replay import CreateReplayRun, CreateReplayRunCommand
from apex.domain.corporate_calibration_depth import CorporateNullMechanism
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
from apex.domain.corporate_regime_followup import (
    CorporateRegimeFollowUpResult,
    CorporateRegimeFollowUpSpecification,
    CorporateRegimeNullControl,
    IncrementalContributionVerdict,
    ParentRelationshipBehavior,
    ParentRelationshipComparison,
    RegimeDimensionSelection,
    RegimeFollowUpConclusion,
    RegimeGroupEffect,
    RegimeGroupEvidence,
    RegimeIncrementalContribution,
    RegimeInteractionResult,
    RegimeSplitResult,
    RegimeSplitRole,
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
from apex.domain.research_calibration_v2 import MatchedCalibrationResult
from apex.domain.research_methodology import (
    BenchmarkKind,
    ResearchConclusion,
    ResearchConclusionState,
    ResearchPhase,
    SampleQualityState,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 10, 15, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _seeded_market_lineage(engine: Engine) -> tuple[str, str, str]:
    """Identical construction to ``test_postgres_corporate_calibration_
    depth._seeded_market_lineage``."""

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
        operator_action_id="crf-test-plan",
        idempotency_key="corporate-regime-followup-postgres-test-v1",
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
            operator_action_id="crf-test-op",
            idempotency_key="crf-test-replay-run",
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
    """Identical construction to ``test_postgres_corporate_calibration_
    depth._seeded_corporate_dataset``."""

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
        objective="Regime follow-up persistence proof agenda.",
        corporate_dataset_id=corporate_dataset_id,
        equity_snapshot_id=equity_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        cohort_policy_id="cohort-policy-test-v1",
        allowed_feature_ids=tuple(CorporateFeatureId),
        allowed_outcome_ids=tuple(CorporateOutcomeId),
        allowed_horizons_sessions=ALLOWED_HORIZONS_SESSIONS,
        allowed_templates=("CROSS_SECTIONAL_V1",),
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


def _hypothesis(agenda: CorporateMarketResearchAgenda) -> GeneratedCorporateHypothesis:
    return GeneratedCorporateHypothesis.create(
        agenda=agenda,
        template_id="CROSS_SECTIONAL_V1",
        template_version="V1",
        family=CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        feature_id=CorporateFeatureId.NET_INCOME_GROWTH_YOY,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        condition_label=None,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        benchmark_kind="EQUAL_WEIGHT_COHORT",
        rationale="Regime follow-up persistence proof hypothesis.",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=False,
        negative_control_kind=None,
        discovery_status=DiscoveryStatus.PREDECLARED,
        ordinal=1,
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
        sample_quality_state=SampleQualityState.SUFFICIENT,
        point_in_time_limitations=(),
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
        beats_benchmark=True,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        rationale="Regime follow-up persistence proof conclusion.",
        blockers=(),
        created_at=NOW,
    )
    return PostgresResearchMethodologyRepository(engine).save_conclusion(conclusion)


def _seeded_followup_lineage(
    engine: Engine,
) -> tuple[
    CorporateMarketResearchAgenda,
    GeneratedCorporateHypothesis,
    ResearchConclusion,
    FollowUpProposal,
]:
    equity_snapshot_id, replay_run_id, cross_market_dataset_id = _seeded_market_lineage(engine)
    corporate_dataset_id = _seeded_corporate_dataset(engine)
    market_repository = PostgresCorporateMarketResearchRepository(engine)
    agenda = market_repository.save_agenda(
        _agenda(equity_snapshot_id, replay_run_id, cross_market_dataset_id, corporate_dataset_id)
    )
    hypothesis = market_repository.save_hypothesis(_hypothesis(agenda))
    conclusion = _real_conclusion(engine, hypothesis, replay_run_id, corporate_dataset_id)
    follow_up = market_repository.save_follow_up(
        FollowUpProposal.create(
            parent_conclusion_id=conclusion.conclusion_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            kind=FollowUpKind.SPLIT_BY_REGIME,
            proposed_template_id="CROSS_SECTIONAL_V1",
            description=(
                "Bounded follow-up for NET_INCOME_GROWTH_YOY / SUBSEQUENT_RETURN: SPLIT_BY_REGIME."
            ),
            created_at=NOW,
        )
    )
    return agenda, hypothesis, conclusion, follow_up


def _selection(role: RegimeSplitRole, dimension: str) -> RegimeDimensionSelection:
    return RegimeDimensionSelection.create(
        split_role=role,
        dimension=dimension,
        group_a_name="REGIME_ADVANCING",
        group_a_labels=("STRONG_UPTREND", "UPTREND"),
        group_b_name="REGIME_NOT_ADVANCING",
        group_b_labels=("RANGE_BOUND", "DOWNTREND", "STRONG_DOWNTREND"),
        excluded_labels=("INSUFFICIENT_EVIDENCE",),
        precedent_hypothesis_id="corporate_market_hypothesis_4d6eb652b767030c07b680a4",
        precedent_feature_id="REVENUE_GROWTH_YOY",
        selection_rationale=(
            "Deterministic sibling-feature precedent, not a scan of all dimensions."
        ),
        null_control_budget=30,
    )


def _specification(
    agenda: CorporateMarketResearchAgenda,
    hypothesis: GeneratedCorporateHypothesis,
    conclusion: ResearchConclusion,
    follow_up: FollowUpProposal,
) -> CorporateRegimeFollowUpSpecification:
    return CorporateRegimeFollowUpSpecification.create(
        follow_up_id=follow_up.follow_up_id,
        parent_hypothesis_id=hypothesis.hypothesis_id,
        parent_conclusion_id=conclusion.conclusion_id,
        parent_reassessment_id="corporate_calibration_reassessment_test",
        parent_qualification_id="research_calibration_v2_follow_up_qualification_test",
        agenda_id=agenda.agenda_id,
        feature_id=CorporateFeatureId.NET_INCOME_GROWTH_YOY,
        feature_transformation="(current - prior) / abs(prior)",
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        market_scope="US_EQUITY; test cohort",
        cohort_policy_id=agenda.cohort_policy_id,
        corporate_dataset_id=agenda.corporate_dataset_id,
        equity_snapshot_id=agenda.equity_snapshot_id,
        replay_run_id=agenda.replay_run_id,
        cross_market_dataset_id=agenda.cross_market_dataset_id,
        original_train_start="2020-07-27",
        original_train_end="2024-08-01",
        original_validation_start="2024-10-01",
        original_validation_end="2025-08-01",
        original_holdout_start="2025-09-02",
        original_holdout_end="2026-07-01",
        original_train_raw_sample=2058,
        original_train_effective_sample=Decimal(62),
        original_validation_raw_sample=349,
        original_validation_effective_sample=Decimal(11),
        original_holdout_raw_sample=341,
        original_holdout_effective_sample=Decimal(11),
        original_conclusion_state="WEAK_SUPPORT",
        original_validation_beats_benchmark=True,
        original_holdout_beats_benchmark=True,
        original_validation_relative_result=Decimal("0.00657659"),
        original_calibrated_grade=EvidenceGrade.SUGGESTIVE,
        matched_null_stratum="CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|NUMERIC_SPLIT",
        matched_null_count=40,
        matched_null_percentile=Decimal("0.95"),
        follow_up_kind="SPLIT_BY_REGIME",
        follow_up_rationale="matched percentile 0.95; evidence grade SUGGESTIVE",
        follow_up_qualification="CALIBRATION_QUALIFIED",
        regime_selections=(
            _selection(RegimeSplitRole.PRIMARY, "EQUITY_TREND"),
            _selection(RegimeSplitRole.SECONDARY, "EQUITY_VOLATILITY"),
        ),
        sample_scope_description="Uses the full retained historical panel.",
        created_at=NOW,
    )


def _null_control(specification_id: str, *, ordinal: int = 0) -> CorporateRegimeNullControl:
    return CorporateRegimeNullControl.create(
        specification_id=specification_id,
        split_role=RegimeSplitRole.PRIMARY,
        ordinal=ordinal,
        seed=20260809 + ordinal * 1000,
        mechanism=CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
        group_a_raw_count=100,
        group_a_effect=Decimal("0.01"),
        group_b_raw_count=400,
        group_b_effect=Decimal("0.02"),
        preserved_properties=("REAL_MARKET_DATES", "REAL_REGIME_GROUP_MEMBERSHIP_AND_PREVALENCE"),
        destroyed_relationship="feature-outcome pairing shuffled within date",
        created_at=NOW,
    )


def _split(role: RegimeSplitRole) -> RegimeSplitResult:
    matched = MatchedCalibrationResult(
        matched_stratum="s",
        matched_null_count=30,
        sufficient=True,
        matched_percentile=Decimal("0.95"),
        pooled_null_count=30,
        pooled_percentile=Decimal("0.95"),
    )
    return RegimeSplitResult(
        role=role,
        selection=_selection(
            role, "EQUITY_TREND" if role is RegimeSplitRole.PRIMARY else "EQUITY_VOLATILITY"
        ),
        group_a_evidence=RegimeGroupEvidence(
            group_name="REGIME_ADVANCING",
            labels=("UPTREND",),
            raw_observations=900,
            distinct_effective_dates=14,
            issuer_count=60,
            sector_count=10,
            missing_feature_rate=Decimal("0.02"),
            sufficient=True,
            insufficiency_reasons=(),
        ),
        group_b_evidence=RegimeGroupEvidence(
            group_name="REGIME_NOT_ADVANCING",
            labels=("RANGE_BOUND",),
            raw_observations=3500,
            distinct_effective_dates=58,
            issuer_count=67,
            sector_count=12,
            missing_feature_rate=Decimal("0.01"),
            sufficient=True,
            insufficiency_reasons=(),
        ),
        adequate=True,
        inadequacy_reason=None,
        group_a_effect=RegimeGroupEffect(
            group_name="REGIME_ADVANCING",
            treatment_count=400,
            benchmark_count=900,
            treatment_mean=Decimal("0.02"),
            benchmark_mean=Decimal("0.01"),
            effect=Decimal("0.01"),
            beats_benchmark=True,
            cohens_d=Decimal("0.1"),
            effective_sample=Decimal(14),
            standard_error=Decimal("0.001"),
            bootstrap_ci_lower=Decimal("-0.01"),
            bootstrap_ci_upper=Decimal("0.03"),
        ),
        group_b_effect=RegimeGroupEffect(
            group_name="REGIME_NOT_ADVANCING",
            treatment_count=1500,
            benchmark_count=3500,
            treatment_mean=Decimal("0.005"),
            benchmark_mean=Decimal("0.004"),
            effect=Decimal("0.001"),
            beats_benchmark=True,
            cohens_d=Decimal("0.01"),
            effective_sample=Decimal(58),
            standard_error=Decimal("0.0005"),
            bootstrap_ci_lower=Decimal("-0.001"),
            bootstrap_ci_upper=Decimal("0.003"),
        ),
        interaction=RegimeInteractionResult(
            interaction_effect=Decimal("0.009"),
            combined_standard_error=Decimal("0.0011"),
            interaction_cohens_d=Decimal("0.25"),
            effect_meets_floor=True,
            effect_floor_policy_id="CORPORATE_REGIME_FOLLOWUP_EFFECT_SIZE_FLOOR_V1_0.20",
            raw_p_value=Decimal("0.05"),
            raw_significant=True,
            matched_calibration=matched,
        ),
        interaction_stability=None,
        robustness=None,
        incremental=RegimeIncrementalContribution(
            regime_only_effect=Decimal("0.001"),
            regime_only_sample=4400,
            corporate_only_effect=Decimal("0.008"),
            corporate_only_sample=4400,
            combined_interaction_effect=Decimal("0.009"),
            combined_sample=1900,
            verdict=IncrementalContributionVerdict.GENUINELY_INCREMENTAL,
            rationale="genuinely incremental",
        ),
        sample_quality=SampleQualityState.SUFFICIENT,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        evidence_grade_rationale="suggestive",
    )


def _result(specification_id: str) -> CorporateRegimeFollowUpResult:
    from apex.domain.corporate_regime_followup import build_knowledge_history

    history = build_knowledge_history(
        hypothesis_id="hyp-1",
        original_conclusion_id="conc-1",
        original_conclusion_state="WEAK_SUPPORT",
        original_conclusion_created_at=NOW,
        reassessment_id="r-1",
        calibrated_grade=EvidenceGrade.SUGGESTIVE,
        reassessment_created_at=NOW,
        follow_up_id="fu-1",
        qualification_id="q-1",
        qualification="CALIBRATION_QUALIFIED",
        qualification_created_at=NOW,
        result_id=specification_id,
        result_conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
        result_created_at=NOW,
    )
    return CorporateRegimeFollowUpResult.create(
        specification_id=specification_id,
        primary_split=_split(RegimeSplitRole.PRIMARY),
        secondary_split=_split(RegimeSplitRole.SECONDARY),
        multiple_testing_results=(),
        parent_relationship=ParentRelationshipComparison(
            original_validation_relative_result=Decimal("0.01"),
            full_sample_unsplit_relative_result=Decimal("0.008"),
            group_a_effect=Decimal("0.01"),
            group_b_effect=Decimal("0.001"),
            behavior=ParentRelationshipBehavior.CONSISTENT,
            rationale="consistent",
        ),
        conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
        conclusion_rationale="genuine regime effect supported",
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        evidence_grade_rationale="suggestive",
        knowledge_history=history,
        created_at=NOW,
    )


def test_corporate_regime_followup_tables_exist_at_head(postgres_engine: Engine) -> None:
    table_names = set(sa.inspect(postgres_engine).get_table_names())
    assert table_names >= set(CORPORATE_REGIME_FOLLOWUP_TABLES)


def test_specification_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda, hypothesis, conclusion, follow_up = _seeded_followup_lineage(postgres_engine)
    repository = PostgresCorporateRegimeFollowUpRepository(postgres_engine)
    specification = _specification(agenda, hypothesis, conclusion, follow_up)
    saved = repository.save_specification(specification)
    repository.save_specification(specification)  # idempotent replay
    assert repository.specification_by_id(specification.specification_id) == saved
    assert repository.specification_by_follow_up(follow_up.follow_up_id) == saved


def test_null_control_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda, hypothesis, conclusion, follow_up = _seeded_followup_lineage(postgres_engine)
    repository = PostgresCorporateRegimeFollowUpRepository(postgres_engine)
    specification = repository.save_specification(
        _specification(agenda, hypothesis, conclusion, follow_up)
    )
    control = _null_control(specification.specification_id, ordinal=0)
    repository.save_null_control(control)
    repository.save_null_control(control)  # idempotent replay
    retained = repository.controls_for_specification(specification.specification_id)
    assert retained == (control,)
    assert repository.controls_for_specification(
        specification.specification_id, role=RegimeSplitRole.PRIMARY
    ) == (control,)
    assert (
        repository.controls_for_specification(
            specification.specification_id, role=RegimeSplitRole.SECONDARY
        )
        == ()
    )


def test_result_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda, hypothesis, conclusion, follow_up = _seeded_followup_lineage(postgres_engine)
    repository = PostgresCorporateRegimeFollowUpRepository(postgres_engine)
    specification = repository.save_specification(
        _specification(agenda, hypothesis, conclusion, follow_up)
    )
    result = _result(specification.specification_id)
    saved = repository.save_result(result)
    repository.save_result(result)  # idempotent replay
    fresh = repository.result_by_specification(specification.specification_id)
    assert fresh == saved
    assert fresh.independent_confirmation_required is True
    assert fresh.conclusion is RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED


def test_immutable_tables_reject_update_and_delete(postgres_engine: Engine) -> None:
    agenda, hypothesis, conclusion, follow_up = _seeded_followup_lineage(postgres_engine)
    repository = PostgresCorporateRegimeFollowUpRepository(postgres_engine)
    specification = repository.save_specification(
        _specification(agenda, hypothesis, conclusion, follow_up)
    )
    with postgres_engine.begin() as connection, pytest.raises(sa.exc.DBAPIError):
        connection.execute(
            sa.text(
                "UPDATE corporate_regime_followup_specification "
                "SET feature_id = 'REVENUE_GROWTH_YOY' WHERE specification_id = :id"
            ),
            {"id": specification.specification_id},
        )
    with postgres_engine.begin() as connection, pytest.raises(sa.exc.DBAPIError):
        connection.execute(
            sa.text(
                "DELETE FROM corporate_regime_followup_specification WHERE specification_id = :id"
            ),
            {"id": specification.specification_id},
        )


def test_retained_evidence_blocks_destructive_0091_downgrade(postgres_engine: Engine) -> None:
    agenda, hypothesis, conclusion, follow_up = _seeded_followup_lineage(postgres_engine)
    PostgresCorporateRegimeFollowUpRepository(postgres_engine).save_specification(
        _specification(agenda, hypothesis, conclusion, follow_up)
    )
    with (
        postgres_engine.connect() as connection,
        pytest.raises(Exception, match="cannot downgrade 0091"),
    ):
        command.downgrade(_config(connection), "0090_corporate_calibration_depth")


def test_corporate_regime_followup_never_touches_crypto_kalshi_or_options_tables(
    postgres_engine: Engine,
) -> None:
    agenda, hypothesis, conclusion, follow_up = _seeded_followup_lineage(postgres_engine)
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
    repository = PostgresCorporateRegimeFollowUpRepository(postgres_engine)
    specification = repository.save_specification(
        _specification(agenda, hypothesis, conclusion, follow_up)
    )
    repository.save_null_control(_null_control(specification.specification_id))
    repository.save_result(_result(specification.specification_id))
    after = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    assert before == after
