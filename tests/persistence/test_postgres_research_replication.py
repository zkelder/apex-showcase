"""PostgreSQL persistence proofs for migration 0084: the six Calibration-
Qualified Interaction Replication v1 tables round-trip, replay
idempotently, enforce immutability, and block a destructive downgrade
while evidence is retained. Also proves ``FreezeReplicationSpecification``
resolves a genuine (if minimal, synthetic) parent lineage end-to-end
against a real database -- exact replay, never a rewrite of the historical
records it reads.
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

from apex.adapters.cross_market_research_postgres import PostgresCrossMarketResearchRepository
from apex.adapters.research_calibration_postgres import PostgresCalibrationRepository
from apex.adapters.research_calibration_v2_postgres import PostgresCalibrationV2Repository
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.adapters.research_replication_postgres import (
    RESEARCH_REPLICATION_TABLES,
    PostgresResearchReplicationRepository,
)
from apex.application.research_replication import FreezeReplicationSpecification
from apex.domain.cross_market_research import (
    ConditionRule,
    DiscoveryStatus,
    FeatureFamily,
    FeatureId,
    FollowUpKind,
    FollowUpProposal,
    GeneratedHypothesis,
    HypothesisExecutionRecord,
    HypothesisFamily,
    HypothesisTemplateId,
    MarketId,
    OutcomeFamily,
    OutcomeId,
    ProposalSource,
)
from apex.domain.historical_replay import (
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
)
from apex.domain.research_calibration import (
    CalibrationPlan,
    EvidenceGrade,
    FollowUpPriorityAssessment,
    NullGenerationMethod,
)
from apex.domain.research_calibration import FollowUpPriorityClass as V1FollowUpPriorityClass
from apex.domain.research_calibration_v2 import (
    ConditioningShape,
    DesignClassification,
    FollowUpQualification,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    ResearchDesign,
)
from apex.domain.research_methodology import (
    GovernanceIntent,
    ResearchConclusion,
    ResearchConclusionState,
    ResearchGovernancePolicy,
    ResearchPhase,
    SampleQualityState,
)
from apex.domain.research_replication import (
    ConfoundDimensionResult,
    PartitionKind,
    ReplicationConclusion,
    ReplicationConclusionState,
    ReplicationConfoundAssessment,
    ReplicationEffectProfile,
    ReplicationPartitionResult,
    ReplicationSpecification,
    TimingAlignmentResult,
    TimingConvention,
    TransactionCostRelevance,
)
from tests.persistence.test_postgres_cross_market_research import _agenda, _seeded_lineage

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _interaction_hypothesis(agenda, ordinal: int = 10) -> GeneratedHypothesis:
    return GeneratedHypothesis.create(
        agenda=agenda,
        template_id=HypothesisTemplateId.INTERACTION_V1,
        template_version="V1",
        feature_id=FeatureId.BTC_RETURN_20,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        condition_label=None,
        secondary_feature_id=FeatureId.EQUITY_VOLATILITY_20,
        secondary_condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        secondary_condition_label=None,
        market_scope=(MarketId.BTC, MarketId.US_EQUITY),
        outcome_id=OutcomeId.BTC_FORWARD_RETURN,
        horizon_sessions=20,
        benchmark_kind="PRIOR_REGIME_DISTRIBUTION",
        family=HypothesisFamily.EQUITY_BTC,
        rationale="Persistence proof interaction hypothesis.",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=False,
        negative_control_kind=None,
        discovery_status=DiscoveryStatus.PREDECLARED,
        ordinal=ordinal,
        created_at=NOW,
    )


def _seeded_parent_lineage(engine: Engine) -> dict[str, object]:
    """Builds exactly the minimal genuine parent chain
    ``FreezeReplicationSpecification`` reads: agenda -> INTERACTION_V1
    hypothesis -> governance policy (with a declared TRAIN period) ->
    execution record -> conclusion -> follow-up proposal -> v1 JUSTIFIED
    priority -> v2 CALIBRATION_QUALIFIED classification/qualification.
    Never a rewrite of anything -- every row is a fresh, real save."""

    snapshot, replay_run_id, dataset_id = _seeded_lineage(engine)
    cross_market_repository = PostgresCrossMarketResearchRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    calibration_v1_repository = PostgresCalibrationRepository(engine)
    calibration_v2_repository = PostgresCalibrationV2Repository(engine)

    agenda = cross_market_repository.save_agenda(
        _agenda(snapshot.snapshot_id, replay_run_id, dataset_id)
    )
    hypothesis = cross_market_repository.save_hypothesis(_interaction_hypothesis(agenda))

    period_policy = ResearchPeriodPolicy.create(
        replay_run_id=replay_run_id,
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2020-07-27", "2023-10-25", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2023-10-26", "2025-08-08", False
            ),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2025-08-11", "2026-06-30", True),
        ),
        minimum_sample_count=8,
        created_at=NOW,
    )
    governance_policy = methodology_repository.save_governance_policy(
        ResearchGovernancePolicy.create(
            hypothesis_version_id=hypothesis.hypothesis_id,
            dataset_snapshot_id=snapshot.snapshot_id,
            replay_run_id=replay_run_id,
            intent=GovernanceIntent.PROMOTION_TRACK,
            period_policy=period_policy,
            declared_at=NOW,
        )
    )

    conclusion = methodology_repository.save_conclusion(
        ResearchConclusion.create(
            hypothesis_version_id=hypothesis.hypothesis_id,
            dataset_snapshot_id=snapshot.snapshot_id,
            replay_run_id=replay_run_id,
            phase=ResearchPhase.LOCKED_HOLDOUT,
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
            rationale="Persistence proof conclusion for the interaction hypothesis.",
            blockers=("INSUFFICIENT_EFFECTIVE_SAMPLE",),
            created_at=NOW,
        )
    )

    cross_market_repository.save_execution(
        HypothesisExecutionRecord.create(
            hypothesis_id=hypothesis.hypothesis_id,
            agenda_id=agenda.agenda_id,
            governance_policy_id=governance_policy.governance_policy_id,
            exploratory_trial_id=None,
            validation_trial_id=None,
            holdout_trial_id=None,
            walk_forward_trial_ids=(),
            robustness_run_id=None,
            trial_family_id=None,
            conclusion_id=conclusion.conclusion_id,
            promotion_evaluation_id=None,
            stability=None,
            evidence_shape="TIME_SERIES_PANEL_WITH_SHARED_MARKET_DATES_NOT_INDEPENDENT_SAMPLES",
            blockers=(),
            created_at=NOW,
        )
    )

    follow_up = cross_market_repository.save_follow_up(
        FollowUpProposal.create(
            parent_conclusion_id=conclusion.conclusion_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            kind=FollowUpKind.SPLIT_BY_REGIME,
            proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
            description="Persistence proof follow-up proposal.",
            created_at=NOW,
        )
    )

    v1_plan = calibration_v1_repository.save_plan(
        CalibrationPlan.create(
            version="V1",
            objective="Persistence proof v1 calibration plan.",
            dataset_snapshot_id=snapshot.snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=dataset_id,
            source_agenda_id=agenda.agenda_id,
            research_methodology_policy_id="RESEARCH_METHODOLOGY_V1",
            allowed_feature_families=tuple(FeatureFamily),
            allowed_outcome_families=tuple(OutcomeFamily),
            allowed_horizons_sessions=(5, 20),
            null_generation_methods=tuple(NullGenerationMethod),
            negative_control_budget=50,
            random_seed_policy="fixed seeds declared a priori",
            multiple_testing_method="BENJAMINI_HOCHBERG_FDR",
            block_length_policy="block lengths {5, 20} declared a priori",
            conclusion_policy_id="RESEARCH_SIGNAL_CALIBRATION_V1",
            registered_at=NOW,
        )
    )
    calibration_v1_repository.save_follow_up_priority(
        FollowUpPriorityAssessment.create(
            calibration_id=v1_plan.calibration_id,
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            classification=V1FollowUpPriorityClass.JUSTIFIED,
            rationale="Persistence proof: v1 JUSTIFIED.",
            created_at=NOW,
        )
    )

    v2_plan = calibration_v1_repository.save_plan(
        CalibrationPlan.create(
            version="V2",
            objective="Persistence proof v2 calibration plan.",
            dataset_snapshot_id=snapshot.snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=dataset_id,
            source_agenda_id=agenda.agenda_id,
            research_methodology_policy_id="RESEARCH_METHODOLOGY_V1+RESEARCH_SIGNAL_CALIBRATION_V1",
            allowed_feature_families=tuple(FeatureFamily),
            allowed_outcome_families=tuple(OutcomeFamily),
            allowed_horizons_sessions=(5, 20),
            null_generation_methods=tuple(NullGenerationMethod),
            negative_control_budget=130,
            random_seed_policy="fixed v2 seeds declared a priori",
            multiple_testing_method="BENJAMINI_HOCHBERG_FDR",
            block_length_policy="block lengths {5, 10, 20, 40} declared a priori",
            conclusion_policy_id="RESEARCH_SIGNAL_CALIBRATION_V2",
            registered_at=NOW,
        )
    )
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("1.39"),
        matched_null_statistics=tuple(Decimal(v) / 10 for v in range(-7, 8)),
        pooled_null_statistics=tuple(Decimal(v) / 10 for v in range(-7, 8)),
        matched_stratum="INTERACTION|BTC_RETURN|H20|BTC+US_EQUITY|NUMERIC_INTERACTION",
    )
    calibration_v2_repository.save_classification(
        DesignClassification.create(
            calibration_id=v2_plan.calibration_id,
            subject_id=hypothesis.hypothesis_id,
            subject_kind="HYPOTHESIS",
            research_design=ResearchDesign.INTERACTION,
            conditioning_shape=ConditioningShape.NUMERIC_INTERACTION,
            matched=matched,
            raw_significant=True,
            corrected_significant=True,
            cohens_d=Decimal("1.39"),
            evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
            created_at=NOW,
        )
    )
    calibration_v2_repository.save_follow_up_qualification(
        FollowUpQualification.create(
            calibration_id=v2_plan.calibration_id,
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            research_design=ResearchDesign.INTERACTION,
            horizon_sessions=hypothesis.horizon_sessions,
            matched=matched,
            evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
            qualification=FollowUpQualificationV2.CALIBRATION_QUALIFIED,
            rationale="Persistence proof: v2 CALIBRATION_QUALIFIED.",
            created_at=NOW,
        )
    )

    return {
        "cross_market_repository": cross_market_repository,
        "methodology_repository": methodology_repository,
        "calibration_v1_repository": calibration_v1_repository,
        "calibration_v2_repository": calibration_v2_repository,
        "follow_up_id": follow_up.follow_up_id,
        "v2_calibration_id": v2_plan.calibration_id,
        "hypothesis": hypothesis,
    }


def test_research_replication_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_REPLICATION_TABLES)


def test_freeze_replication_specification_resolves_genuine_parent_lineage(
    postgres_engine: Engine,
) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    replication_repository = PostgresResearchReplicationRepository(postgres_engine)
    specification = FreezeReplicationSpecification(
        cross_market_repository=lineage["cross_market_repository"],
        methodology_repository=lineage["methodology_repository"],
        calibration_v1_repository=lineage["calibration_v1_repository"],
        calibration_v2_repository=lineage["calibration_v2_repository"],
        replication_repository=replication_repository,
    ).execute(
        follow_up_id=lineage["follow_up_id"],
        v2_calibration_id=lineage["v2_calibration_id"],
        created_at=NOW,
    )
    assert specification.parent_hypothesis_id == lineage["hypothesis"].hypothesis_id
    assert specification.train_period_start == "2020-07-27"
    assert specification.train_period_end == "2023-10-25"
    assert specification.research_design is ResearchDesign.INTERACTION
    assert specification.v2_matched_percentile == Decimal("1")

    # Exact replay: re-freezing is a pure read plus an idempotent save --
    # calling it again returns the byte-identical specification.
    replayed = FreezeReplicationSpecification(
        cross_market_repository=lineage["cross_market_repository"],
        methodology_repository=lineage["methodology_repository"],
        calibration_v1_repository=lineage["calibration_v1_repository"],
        calibration_v2_repository=lineage["calibration_v2_repository"],
        replication_repository=replication_repository,
    ).execute(
        follow_up_id=lineage["follow_up_id"],
        v2_calibration_id=lineage["v2_calibration_id"],
        created_at=NOW,
    )
    assert replayed.replication_id == specification.replication_id
    assert replayed.canonical_hash == specification.canonical_hash

    fresh = PostgresResearchReplicationRepository(postgres_engine)
    reloaded = fresh.specification_by_id(specification.replication_id)
    assert reloaded == specification
    by_follow_up = fresh.specification_by_follow_up(lineage["follow_up_id"])
    assert by_follow_up == specification


def _spec_from(lineage: dict[str, object], engine: Engine) -> ReplicationSpecification:
    replication_repository = PostgresResearchReplicationRepository(engine)
    return FreezeReplicationSpecification(
        cross_market_repository=lineage["cross_market_repository"],
        methodology_repository=lineage["methodology_repository"],
        calibration_v1_repository=lineage["calibration_v1_repository"],
        calibration_v2_repository=lineage["calibration_v2_repository"],
        replication_repository=replication_repository,
    ).execute(
        follow_up_id=lineage["follow_up_id"],
        v2_calibration_id=lineage["v2_calibration_id"],
        created_at=NOW,
    )


def test_specification_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    spec = _spec_from(lineage, postgres_engine)
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE research_replication_specification SET horizon_sessions = 5 "
                "WHERE replication_id = :id"
            ),
            {"id": spec.replication_id},
        )


def test_timing_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    spec = _spec_from(lineage, postgres_engine)
    repository = PostgresResearchReplicationRepository(postgres_engine)
    timing = TimingAlignmentResult.create(
        replication_id=spec.replication_id,
        convention=TimingConvention.PRIMARY_SAME_DAY_V1,
        ordering_valid=True,
        checked_date_count=1489,
        violation_count=0,
        sample_count=249,
        effective_sample_count=Decimal("12.45"),
        benchmark_sample_count=507,
        interaction_effect=Decimal("0.023"),
        cohens_d=Decimal("0.115"),
        note="test",
        created_at=NOW,
    )
    repository.save_timing(timing)
    repository.save_timing(timing)  # idempotent replay, never a conflict
    fresh = PostgresResearchReplicationRepository(postgres_engine)
    reloaded = fresh.timings_for_replication(spec.replication_id)
    assert reloaded == (timing,)


def test_partition_round_trips_full_sample_and_finer_slices(postgres_engine: Engine) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    spec = _spec_from(lineage, postgres_engine)
    repository = PostgresResearchReplicationRepository(postgres_engine)
    full = ReplicationPartitionResult.create(
        replication_id=spec.replication_id,
        partition_kind=PartitionKind.FULL_SAMPLE,
        label="FULL_SAMPLE",
        start_date="2020-07-27",
        end_date="2026-06-30",
        sample_count=249,
        effective_sample_count=Decimal("12.45"),
        benchmark_sample_count=507,
        sample_mean=Decimal("0.081"),
        benchmark_mean=Decimal("0.058"),
        interaction_effect=Decimal("0.023"),
        cohens_d=Decimal("0.115"),
        beats_benchmark=True,
        matched_null_count=15,
        matched_percentile=Decimal("1"),
        calibration_note="test",
        created_at=NOW,
    )
    early = ReplicationPartitionResult.create(
        replication_id=spec.replication_id,
        partition_kind=PartitionKind.TIME_PERIOD,
        label="EARLY",
        start_date="2020-07-27",
        end_date="2022-07-14",
        sample_count=105,
        effective_sample_count=Decimal("5.25"),
        benchmark_sample_count=220,
        sample_mean=Decimal("0.02"),
        benchmark_mean=Decimal("0.013"),
        interaction_effect=Decimal("0.007"),
        cohens_d=Decimal("0.026"),
        beats_benchmark=True,
        matched_null_count=0,
        matched_percentile=None,
        calibration_note="descriptive only -- no per-partition matched-null battery",
        created_at=NOW,
    )
    repository.save_partition(full)
    repository.save_partition(early)
    fresh = PostgresResearchReplicationRepository(postgres_engine)
    reloaded = {item.label: item for item in fresh.partitions_for_replication(spec.replication_id)}
    assert reloaded["FULL_SAMPLE"] == full
    assert reloaded["EARLY"] == early
    assert reloaded["EARLY"].matched_percentile is None


def test_effect_profile_and_confound_assessment_round_trip(postgres_engine: Engine) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    spec = _spec_from(lineage, postgres_engine)
    repository = PostgresResearchReplicationRepository(postgres_engine)
    profile = ReplicationEffectProfile.create(
        replication_id=spec.replication_id,
        unconditional_mean=Decimal("0.04"),
        unconditional_sample_count=1469,
        low_vol_mean=Decimal("0.058"),
        low_vol_sample_count=507,
        low_vol_median=Decimal("0.005"),
        low_vol_hit_rate=Decimal("0.55"),
        low_vol_stdev=Decimal("0.2"),
        high_vol_mean=Decimal("0.081"),
        high_vol_sample_count=249,
        high_vol_median=Decimal("0.066"),
        high_vol_hit_rate=Decimal("0.60"),
        high_vol_stdev=Decimal("0.19"),
        high_vol_p10=Decimal("-0.1"),
        high_vol_p90=Decimal("0.3"),
        interaction_effect=Decimal("0.023"),
        cohens_d=Decimal("0.115"),
        benchmark_relative_effect=Decimal("0.023"),
        practical_effect_floor=Decimal("0.20"),
        winsorized_interaction_effect=Decimal("0.034"),
        robust_median_interaction_effect=Decimal("0.061"),
        leave_one_third_out_effects=(("EXCLUDE_EARLY", Decimal("0.007")),),
        holdout_window_excluded_effect=Decimal("0.003"),
        most_influential_dates=("2020-11-27", "2020-12-01"),
        outlier_robust=False,
        outlier_dependence_note="shrinks by more than half under holdout-window exclusion",
        created_at=NOW,
    )
    confound = ReplicationConfoundAssessment.create(
        replication_id=spec.replication_id,
        dimensions=(
            ConfoundDimensionResult(
                dimension="BTC_VOLATILITY",
                dominant_trial_label="HIGH_VOLATILITY",
                trial_concentration=Decimal("0.86"),
                dominant_benchmark_label="HIGH_VOLATILITY",
                benchmark_concentration=Decimal("0.40"),
                same_dominant_label=True,
                effect_within_dominant_label=Decimal("0.008"),
                dominant_label_sample_count=214,
                confound_plausible=True,
                note="test",
            ),
        ),
        note="test",
        created_at=NOW,
    )
    repository.save_effect_profile(profile)
    repository.save_confound_assessment(confound)
    fresh = PostgresResearchReplicationRepository(postgres_engine)
    assert fresh.effect_profile_for_replication(spec.replication_id) == profile
    reloaded_confound = fresh.confound_assessment_for_replication(spec.replication_id)
    assert reloaded_confound == confound
    assert reloaded_confound.overall_confound_resolved is False


def test_conclusion_round_trips_and_references_but_never_rewrites_original_conclusion(
    postgres_engine: Engine,
) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    spec = _spec_from(lineage, postgres_engine)
    repository = PostgresResearchReplicationRepository(postgres_engine)
    conclusion = ReplicationConclusion.create(
        replication_id=spec.replication_id,
        follow_up_id=spec.follow_up_id,
        parent_hypothesis_id=spec.parent_hypothesis_id,
        conclusion_state=ReplicationConclusionState.CONFOUNDING_NOT_RESOLVED,
        evidence_grade=EvidenceGrade.EXPLORATORY,
        evidence_grade_upgraded=False,
        period_stability_note="test",
        volatility_stability_note="test",
        timing_note="test",
        matched_null_note="test",
        effect_size_note="test",
        outlier_robustness_note="test",
        confound_note="test",
        transaction_cost_relevance=TransactionCostRelevance.COMPARABLE_TO_COST_SCALE,
        forward_confirmation_required=True,
        forward_confirmation_reason="test",
        minimum_forward_observation_policy="test",
        limitations=("REGIME_CONFOUND_NOT_RESOLVED",),
        rationale="test",
        created_at=NOW,
    )
    repository.save_conclusion(conclusion)
    fresh = PostgresResearchReplicationRepository(postgres_engine)
    reloaded = fresh.conclusion_for_replication(spec.replication_id)
    assert reloaded == conclusion

    # The original ResearchConclusion this replication traces back to is
    # completely untouched -- a genuinely different, still-retained row.
    original = lineage["methodology_repository"].conclusion_by_id(spec.parent_conclusion_id)
    assert original is not None
    assert original.conclusion_state is ResearchConclusionState.INSUFFICIENT_EVIDENCE
    assert original.rationale == "Persistence proof conclusion for the interaction hypothesis."


def test_conclusion_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    spec = _spec_from(lineage, postgres_engine)
    repository = PostgresResearchReplicationRepository(postgres_engine)
    conclusion = ReplicationConclusion.create(
        replication_id=spec.replication_id,
        follow_up_id=spec.follow_up_id,
        parent_hypothesis_id=spec.parent_hypothesis_id,
        conclusion_state=ReplicationConclusionState.REPLICATED,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        evidence_grade_upgraded=False,
        period_stability_note="test",
        volatility_stability_note="test",
        timing_note="test",
        matched_null_note="test",
        effect_size_note="test",
        outlier_robustness_note="test",
        confound_note="test",
        transaction_cost_relevance=TransactionCostRelevance.COMPARABLE_TO_COST_SCALE,
        forward_confirmation_required=True,
        forward_confirmation_reason="test",
        minimum_forward_observation_policy="test",
        limitations=(),
        rationale="test",
        created_at=NOW,
    )
    repository.save_conclusion(conclusion)
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE research_replication_conclusion SET conclusion_state = 'NULL_COMPATIBLE' "
                "WHERE conclusion_id = :id"
            ),
            {"id": conclusion.conclusion_id},
        )


# --------------------------------------------------------------------------
# Migration downgrade protection
# --------------------------------------------------------------------------


def test_retained_evidence_blocks_destructive_0084_downgrade(postgres_engine: Engine) -> None:
    lineage = _seeded_parent_lineage(postgres_engine)
    _spec_from(lineage, postgres_engine)
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0084 with retained research-replication evidence",
        ):
            command.downgrade(_config(connection), "0083_calibration_v2_template")
        connection.rollback()
    with postgres_engine.connect() as connection:
        # A newer 0085 has since landed on top of 0084: downgrading from
        # head first passes cleanly through 0085 -> 0084 (no options
        # quote-intelligence evidence retained in this test) before
        # failing at the exact 0084 -> 0083 hop this test seeds evidence
        # against, so the resting revision is genuinely 0084, not head.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0084_research_replication"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0084_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_REPLICATION_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0083_calibration_v2_template")
    assert set(RESEARCH_REPLICATION_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_REPLICATION_TABLES)


# --------------------------------------------------------------------------
# Zero provider writes / knowledge-history append-only guarantee
# --------------------------------------------------------------------------


def test_replication_never_creates_follow_up_or_hypothesis_rows(postgres_engine: Engine) -> None:
    """Section 21/26: the replication only ever *reads* the historical
    cross-market-research/calibration surfaces and appends its own new
    rows -- it must never insert another ``FollowUpProposal``,
    ``GeneratedHypothesis``, or ``ResearchConclusion``."""

    lineage = _seeded_parent_lineage(postgres_engine)
    with postgres_engine.connect() as connection:
        follow_ups_before = connection.scalar(
            sa.text("SELECT COUNT(*) FROM cross_market_follow_up_proposal")
        )
        hypotheses_before = connection.scalar(
            sa.text("SELECT COUNT(*) FROM cross_market_hypothesis")
        )
        conclusions_before = connection.scalar(sa.text("SELECT COUNT(*) FROM research_conclusion"))
    _spec_from(lineage, postgres_engine)
    with postgres_engine.connect() as connection:
        follow_ups_after = connection.scalar(
            sa.text("SELECT COUNT(*) FROM cross_market_follow_up_proposal")
        )
        hypotheses_after = connection.scalar(
            sa.text("SELECT COUNT(*) FROM cross_market_hypothesis")
        )
        conclusions_after = connection.scalar(sa.text("SELECT COUNT(*) FROM research_conclusion"))
    assert follow_ups_after == follow_ups_before
    assert hypotheses_after == hypotheses_before
    assert conclusions_after == conclusions_before
