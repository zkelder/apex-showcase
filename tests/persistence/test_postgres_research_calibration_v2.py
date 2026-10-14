"""PostgreSQL persistence proofs for migration 0083: the six genuinely
new Research Calibration v2 tables -- template-matched null-control
specs, their outcomes, design classifications, time-period/volatility-
regime stability slices, walk-forward-null summaries, and follow-up
requalifications -- round-trip, idempotent replay, immutable-evidence
enforcement, and downgrade protection. Plan/run/report/breakdown/method-
comparison/prior-recalibration/calibrated-promotion reuse migration
0082's existing tables unmodified and are proved by the existing v1
persistence suite.
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

from apex.adapters.research_calibration_postgres import PostgresCalibrationRepository
from apex.adapters.research_calibration_v2_postgres import (
    RESEARCH_CALIBRATION_V2_TABLES,
    PostgresCalibrationV2Repository,
)
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.domain.cross_market_research import (
    FeatureFamily,
    FollowUpKind,
    FollowUpProposal,
    HypothesisTemplateId,
    KnowledgeStatus,
    MarketId,
    OutcomeFamily,
)
from apex.domain.research_calibration import (
    CalibrationPlan,
    NullControlDesignFamily,
    NullControlFamily,
    NullControlNature,
    NullControlOutcome,
    NullGenerationMethod,
)
from apex.domain.research_calibration_v2 import (
    ConditioningShape,
    DesignClassification,
    EvidenceGrade,
    FollowUpQualification,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    NullControlSpecV2,
    ResearchDesign,
    StabilitySlice,
    WalkForwardNullSummary,
)
from apex.domain.research_methodology import (
    PromotionEvaluation,
    PromotionPolicy,
    ResearchConclusion,
    ResearchConclusionState,
    ResearchPhase,
    SampleQualityState,
)
from tests.persistence.test_postgres_cross_market_research import (
    _agenda,
    _hypothesis,
    _real_conclusion,
    _seeded_lineage,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seeded_agenda(engine: Engine) -> tuple[str, str, str, str]:
    from apex.adapters.cross_market_research_postgres import PostgresCrossMarketResearchRepository

    snapshot, replay_run_id, dataset_id = _seeded_lineage(engine)
    repository = PostgresCrossMarketResearchRepository(engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    return agenda.agenda_id, snapshot.snapshot_id, replay_run_id, dataset_id


def _v1_plan(
    agenda_id: str, snapshot_id: str, replay_run_id: str, dataset_id: str
) -> CalibrationPlan:
    return CalibrationPlan.create(
        version="V1",
        objective="v2 persistence proof: fake v1 lineage anchor.",
        dataset_snapshot_id=snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=dataset_id,
        source_agenda_id=agenda_id,
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


def _v2_plan(
    agenda_id: str, snapshot_id: str, replay_run_id: str, dataset_id: str
) -> CalibrationPlan:
    return CalibrationPlan.create(
        version="V2",
        objective="v2 persistence proof calibration plan. Extends fake_v1.",
        dataset_snapshot_id=snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=dataset_id,
        source_agenda_id=agenda_id,
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


def _spec_v2(calibration: CalibrationPlan, ordinal: int = 1) -> NullControlSpecV2:
    return NullControlSpecV2.create(
        calibration_id=calibration.calibration_id,
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        family=NullControlFamily.DETERMINISTIC_PERMUTATION,
        nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
        design_family=NullControlDesignFamily.INTERACTION_CONDITIONAL,
        control_kind="CONDITIONAL_OUTCOME_NULLED",
        null_method=NullGenerationMethod.BLOCK_PERMUTATION,
        null_target="OUTCOME",
        source_feature_id="BTC_RETURN_20",
        source_feature_family=FeatureFamily.BTC_MOMENTUM,
        secondary_feature_id="BTC_EQUITY_CORRELATION_60",
        secondary_condition_rule="ABOVE_TRAIN_MEDIAN",
        secondary_condition_label=None,
        outcome_id="BTC_FORWARD_RETURN",
        outcome_family=OutcomeFamily.BTC_RETURN,
        horizon_sessions=20,
        market_scope=(MarketId.BTC, MarketId.US_EQUITY),
        method_params=(("block_length", "5"), ("seed", "1")),
        rationale="Persistence proof v2 null control.",
        ordinal=ordinal,
        created_at=NOW,
    )


def test_research_calibration_v2_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_CALIBRATION_V2_TABLES)


def test_v2_plan_reuses_the_0082_plan_table(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    v1_plan = repository.save_plan(_v1_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    v2_plan = repository.save_plan(_v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    assert v1_plan.calibration_id != v2_plan.calibration_id
    fresh = PostgresCalibrationRepository(postgres_engine)
    assert fresh.plan_by_id(v1_plan.calibration_id) == v1_plan
    assert fresh.plan_by_id(v2_plan.calibration_id) == v2_plan


def test_v2_null_control_spec_round_trips_and_lists_by_calibration_in_ordinal_order(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    second = repository.save_spec(_spec_v2(plan, ordinal=2))
    first = repository.save_spec(_spec_v2(plan, ordinal=1))
    fresh = PostgresCalibrationV2Repository(postgres_engine)
    listed = fresh.specs_for_calibration(plan.calibration_id)
    assert [item.spec_id for item in listed] == [first.spec_id, second.spec_id]
    assert fresh.spec_by_id(first.spec_id) == first
    assert fresh.specs_for_stratum(first.matched_stratum) != ()
    # save is idempotent
    assert repository.save_spec(first) == first


def test_v2_null_control_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    spec = repository.save_spec(_spec_v2(plan))
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE research_calibration_v2_null_control SET ordinal = 99 "
                "WHERE spec_id = :spec_id"
            ),
            {"spec_id": spec.spec_id},
        )


def _governed_null_outcome(
    engine: Engine, plan: CalibrationPlan, spec: NullControlSpecV2, calibration_run_id: str
) -> NullControlOutcome:
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    conclusion = methodology_repository.save_conclusion(
        ResearchConclusion.create(
            hypothesis_version_id=spec.spec_id,
            dataset_snapshot_id=plan.dataset_snapshot_id,
            replay_run_id=plan.replay_run_id,
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
            rationale="v2 persistence proof conclusion.",
            blockers=(),
            created_at=NOW,
        )
    )
    promotion = methodology_repository.save_promotion_evaluation(
        PromotionEvaluation.create(
            conclusion=conclusion,
            policy=PromotionPolicy(),
            holdout_exposed=False,
            robustness_completed=False,
            lookahead_violation=False,
            created_at=NOW,
        )
    )
    return NullControlOutcome.create(
        spec_id=spec.spec_id,
        calibration_run_id=calibration_run_id,
        governance_policy_id="governance_policy_fake_for_v2_persistence_proof",
        exploratory_trial_id=None,
        validation_trial_id=None,
        holdout_trial_id=None,
        robustness_run_id=None,
        conclusion_id=conclusion.conclusion_id,
        promotion_evaluation_id=promotion.promotion_evaluation_id,
        knowledge_status=KnowledgeStatus.ACTIVE_RESEARCH,
        blockers=("VALIDATION_SAMPLE_INSUFFICIENT",),
        created_at=NOW,
    )


def test_v2_null_outcome_round_trips_with_real_conclusion_fk(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    spec = repository.save_spec(_spec_v2(plan))
    run = calibration_repository.save_run(
        __import__(
            "apex.domain.research_calibration", fromlist=["CalibrationRun"]
        ).CalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    )
    outcome = _governed_null_outcome(postgres_engine, plan, spec, run.calibration_run_id)
    saved = repository.save_outcome(outcome)
    fresh = PostgresCalibrationV2Repository(postgres_engine)
    assert fresh.outcome_by_spec(spec.spec_id) == saved
    assert saved in fresh.outcomes_for_run(run.calibration_run_id)
    assert repository.save_outcome(outcome) == outcome


def test_v2_null_outcome_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    spec = repository.save_spec(_spec_v2(plan))
    from apex.domain.research_calibration import CalibrationRun

    run = calibration_repository.save_run(
        CalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    )
    outcome = repository.save_outcome(
        _governed_null_outcome(postgres_engine, plan, spec, run.calibration_run_id)
    )
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE research_calibration_v2_null_outcome SET knowledge_status = 'WEAK' "
                "WHERE outcome_id = :outcome_id"
            ),
            {"outcome_id": outcome.outcome_id},
        )


def _matched(sufficient: bool) -> MatchedCalibrationResult:
    count = 15 if sufficient else 2
    nulls = tuple(Decimal(i) for i in range(count))
    return MatchedCalibrationResult.evaluate(
        statistic=Decimal("3"),
        matched_null_statistics=nulls,
        pooled_null_statistics=nulls,
        matched_stratum="CONDITIONAL|BTC_RETURN|H20|BTC+US_EQUITY|NUMERIC_SPLIT",
    )


def test_design_classification_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    spec = repository.save_spec(_spec_v2(plan))
    classification = DesignClassification.create(
        calibration_id=plan.calibration_id,
        subject_id=spec.spec_id,
        subject_kind="NULL_CONTROL",
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        matched=_matched(sufficient=True),
        raw_significant=True,
        corrected_significant=False,
        cohens_d=Decimal("0.3"),
        evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
        created_at=NOW,
    )
    saved = repository.save_classification(classification)
    fresh = PostgresCalibrationV2Repository(postgres_engine)
    assert (
        fresh.classification_for_subject(
            calibration_id=plan.calibration_id, subject_id=spec.spec_id
        )
        == saved
    )
    assert saved in fresh.classifications_for_calibration(plan.calibration_id)
    assert repository.save_classification(classification) == classification


def test_design_classification_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    spec = repository.save_spec(_spec_v2(plan))
    classification = repository.save_classification(
        DesignClassification.create(
            calibration_id=plan.calibration_id,
            subject_id=spec.spec_id,
            subject_kind="NULL_CONTROL",
            research_design=ResearchDesign.CONDITIONAL,
            conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
            matched=_matched(sufficient=False),
            raw_significant=None,
            corrected_significant=None,
            cohens_d=None,
            evidence_grade_v2=EvidenceGrade.EXPLORATORY,
            created_at=NOW,
        )
    )
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE research_calibration_v2_design_classification "
                "SET evidence_grade_v2 = 'STRONG' WHERE classification_id = :id"
            ),
            {"id": classification.classification_id},
        )


def test_stability_slice_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    slice_ = StabilitySlice.create(
        calibration_id=plan.calibration_id,
        dimension_kind="TIME_PERIOD",
        dimension_label="EARLY",
        control_count=25,
        raw_positive_count=3,
        corrected_positive_count=0,
        minimum_adequate_count=10,
        note="Persistence proof slice.",
        created_at=NOW,
    )
    saved = repository.save_stability_slice(slice_)
    fresh = PostgresCalibrationV2Repository(postgres_engine)
    assert saved in fresh.stability_slices_for_calibration(plan.calibration_id)
    assert repository.save_stability_slice(slice_) == slice_


def test_walk_forward_null_summary_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    spec = repository.save_spec(_spec_v2(plan))
    summary = WalkForwardNullSummary.create(
        calibration_id=plan.calibration_id,
        spec_id=spec.spec_id,
        fold_count=3,
        folds_beating_benchmark=1,
        validation_survived=False,
        holdout_survived=False,
        robustness_survived=True,
        final_evidence_grade=EvidenceGrade.EXPLORATORY,
        created_at=NOW,
    )
    saved = repository.save_walk_forward_summary(summary)
    fresh = PostgresCalibrationV2Repository(postgres_engine)
    assert saved in fresh.walk_forward_summaries_for_calibration(plan.calibration_id)
    assert repository.save_walk_forward_summary(summary) == summary


def test_follow_up_qualification_round_trips_and_references_the_historical_proposal(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    from apex.adapters.cross_market_research_postgres import PostgresCrossMarketResearchRepository

    cross_market_repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = cross_market_repository.agenda_by_id(agenda_id)
    hypothesis = cross_market_repository.save_hypothesis(_hypothesis(agenda))
    conclusion = _real_conclusion(postgres_engine, hypothesis, replay_run_id, snapshot_id)
    follow_up = cross_market_repository.save_follow_up(
        FollowUpProposal.create(
            parent_conclusion_id=conclusion.conclusion_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            kind=FollowUpKind.SPLIT_BY_REGIME,
            proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
            description="v2 persistence proof follow-up.",
            created_at=NOW,
        )
    )
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    qualification = FollowUpQualification.create(
        calibration_id=plan.calibration_id,
        follow_up_id=follow_up.follow_up_id,
        parent_hypothesis_id=hypothesis.hypothesis_id,
        research_design=ResearchDesign.CONDITIONAL,
        horizon_sessions=20,
        matched=_matched(sufficient=True),
        evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
        qualification=FollowUpQualificationV2.CALIBRATION_QUALIFIED,
        rationale="Persistence proof qualification.",
        created_at=NOW,
    )
    before_hash = follow_up.canonical_hash
    saved = repository.save_follow_up_qualification(qualification)
    fresh = PostgresCalibrationV2Repository(postgres_engine)
    assert saved in fresh.follow_up_qualifications_for_calibration(plan.calibration_id)
    assert repository.save_follow_up_qualification(qualification) == qualification
    reloaded_follow_up = next(
        item
        for item in cross_market_repository.list_follow_ups()
        if item.follow_up_id == follow_up.follow_up_id
    )
    assert reloaded_follow_up.canonical_hash == before_hash  # historical row untouched


def test_follow_up_qualification_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    from apex.adapters.cross_market_research_postgres import PostgresCrossMarketResearchRepository

    cross_market_repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = cross_market_repository.agenda_by_id(agenda_id)
    hypothesis = cross_market_repository.save_hypothesis(_hypothesis(agenda))
    conclusion = _real_conclusion(postgres_engine, hypothesis, replay_run_id, snapshot_id)
    follow_up = cross_market_repository.save_follow_up(
        FollowUpProposal.create(
            parent_conclusion_id=conclusion.conclusion_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            kind=FollowUpKind.SPLIT_BY_REGIME,
            proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
            description="v2 persistence proof follow-up.",
            created_at=NOW,
        )
    )
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    qualification = repository.save_follow_up_qualification(
        FollowUpQualification.create(
            calibration_id=plan.calibration_id,
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            research_design=ResearchDesign.CONDITIONAL,
            horizon_sessions=20,
            matched=_matched(sufficient=False),
            evidence_grade_v2=EvidenceGrade.EXPLORATORY,
            qualification=FollowUpQualificationV2.CALIBRATION_INSUFFICIENT,
            rationale="Persistence proof qualification.",
            created_at=NOW,
        )
    )
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE research_calibration_v2_follow_up_qualification "
                "SET qualification = 'REJECTED' WHERE assessment_id = :id"
            ),
            {"id": qualification.assessment_id},
        )


# --------------------------------------------------------------------------
# Migration downgrade protection
# --------------------------------------------------------------------------


def test_retained_v2_evidence_blocks_destructive_0083_downgrade(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _v2_plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )
    repository = PostgresCalibrationV2Repository(postgres_engine)
    repository.save_spec(_spec_v2(plan))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0083 with retained template-matched-calibration evidence",
        ):
            command.downgrade(_config(connection), "0082_research_signal_calibration")
        connection.rollback()
    with postgres_engine.connect() as connection:
        # Each migration step commits independently: the downgrade walk from
        # head reached 0083 (0084's own downgrade has no blocking evidence
        # here) before 0083's own guard raised and rolled back -- so the
        # retained state is 0083's revision, not head, until re-upgraded below.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0083_calibration_v2_template"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0083_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_CALIBRATION_V2_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0082_research_signal_calibration")
    assert set(RESEARCH_CALIBRATION_V2_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_CALIBRATION_V2_TABLES)
