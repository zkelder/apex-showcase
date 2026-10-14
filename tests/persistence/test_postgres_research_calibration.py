"""PostgreSQL persistence proofs for migration 0082: the calibration
plan, its declared null-control battery, the one bounded mutable
calibration-run cursor, retained null-control outcomes,
family/horizon/time-period/volatility-regime breakdowns, the statistical-
method comparison, the calibration report, prior-agenda recalibration,
follow-up-priority classification, and the additive calibrated-promotion
gate -- round-trip, idempotent replay, immutable-evidence enforcement,
mutable-cursor updates, and downgrade protection.
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

from apex.adapters.research_calibration_postgres import (
    RESEARCH_CALIBRATION_TABLES,
    PostgresCalibrationRepository,
)
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.domain.cross_market_research import FeatureFamily, KnowledgeStatus, OutcomeFamily
from apex.domain.research_calibration import (
    CALIBRATED_PROMOTION_POLICY,
    CalibratedPromotionEvaluation,
    CalibrationBreakdown,
    CalibrationPlan,
    CalibrationRateSummary,
    CalibrationReport,
    CalibrationRun,
    EvidenceGrade,
    FollowUpPriorityAssessment,
    FollowUpPriorityClass,
    NullControlDesignFamily,
    NullControlFamily,
    NullControlNature,
    NullControlOutcome,
    NullControlSpec,
    NullGenerationMethod,
    PriorHypothesisRecalibration,
    StatisticalMethodComparison,
)
from apex.domain.research_methodology import (
    APEX_PRODUCTION_PROMOTION_POLICY,
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
NOW = datetime(2026, 8, 7, 16, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seeded_agenda(engine: Engine) -> str:
    """Builds the minimum genuine lineage migration 0082's ``source_agenda_id``
    foreign key requires: the full 0081 chain plus one retained
    ``ResearchAgenda`` row, reusing the exact same helpers migration 0081's
    own persistence tests already established."""

    from apex.adapters.cross_market_research_postgres import PostgresCrossMarketResearchRepository

    snapshot, replay_run_id, dataset_id = _seeded_lineage(engine)
    repository = PostgresCrossMarketResearchRepository(engine)
    agenda = repository.save_agenda(_agenda(snapshot.snapshot_id, replay_run_id, dataset_id))
    return agenda.agenda_id, snapshot.snapshot_id, replay_run_id, dataset_id


def _plan(agenda_id: str, snapshot_id: str, replay_run_id: str, dataset_id: str) -> CalibrationPlan:
    return CalibrationPlan.create(
        version="V1",
        objective="Persistence proof calibration plan.",
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


def _spec(calibration: CalibrationPlan, ordinal: int = 1) -> NullControlSpec:
    return NullControlSpec.create(
        calibration=calibration,
        family=NullControlFamily.DETERMINISTIC_PERMUTATION,
        nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
        design_family=NullControlDesignFamily.PURE_MATHEMATICAL_NULL,
        control_kind="BLOCK_PERMUTED_FEATURE",
        null_method=NullGenerationMethod.BLOCK_PERMUTATION,
        source_feature_id="EQUITY_BREADTH_FRACTION_ADVANCING",
        source_feature_family=FeatureFamily.EQUITY_BREADTH,
        outcome_id="EQUITY_FORWARD_RETURN",
        outcome_family=OutcomeFamily.EQUITY_BROAD_RETURN,
        horizon_sessions=20,
        method_params=(("block_length", "5"), ("seed", "1")),
        rationale="Persistence proof null control.",
        ordinal=ordinal,
        created_at=NOW,
    )


def _rate_summary() -> CalibrationRateSummary:
    return CalibrationRateSummary.compute(
        total_controls=50,
        validation_candidates=50,
        raw_positive_findings=20,
        bh_fdr_survivors=0,
        holdout_survivors=22,
        robustness_survivors=45,
        weak_count=8,
        supported_count=0,
        promotion_eligible_count=0,
    )


def test_research_calibration_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_CALIBRATION_TABLES)


def test_plan_round_trips_and_save_is_idempotent(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = _plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    first = repository.save_plan(plan)
    second = repository.save_plan(plan)
    assert first.calibration_id == second.calibration_id == plan.calibration_id
    fresh = PostgresCalibrationRepository(postgres_engine)
    loaded = fresh.plan_by_id(plan.calibration_id)
    assert loaded == plan
    assert loaded in fresh.list_plans()


def test_null_control_spec_round_trips_and_lists_by_calibration_in_ordinal_order(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    second = repository.save_spec(_spec(plan, ordinal=2))
    first = repository.save_spec(_spec(plan, ordinal=1))
    fresh = PostgresCalibrationRepository(postgres_engine)
    listed = fresh.specs_for_calibration(plan.calibration_id)
    assert [item.spec_id for item in listed] == [first.spec_id, second.spec_id]
    assert fresh.spec_by_id(first.spec_id) == first


def test_calibration_run_is_the_one_mutable_table_and_updates_in_place(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    spec = repository.save_spec(_spec(plan))
    run = CalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    repository.save_run(run)
    advanced = run.advance(spec_id=spec.spec_id, trials_added=3, is_final=True, at=NOW)
    repository.save_run(advanced)
    fresh = PostgresCalibrationRepository(postgres_engine)
    loaded = fresh.run_by_calibration(plan.calibration_id)
    assert loaded == advanced
    assert loaded.calibration_run_id == run.calibration_run_id
    assert fresh.run_by_id(run.calibration_run_id) == advanced


def test_calibration_run_is_not_protected_by_the_immutable_trigger(postgres_engine: Engine) -> None:
    """The one deliberate exception: direct UPDATE on the mutable cursor
    table must succeed at the database level, exactly mirroring migration
    0081's own ``cross_market_agenda_run``."""

    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    spec = repository.save_spec(_spec(plan))
    run = CalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    repository.save_run(run)
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE research_calibration_run SET cursor_ordinal = 1 "
                "WHERE calibration_run_id = :run_id"
            ),
            {"run_id": run.calibration_run_id},
        )


def _governed_null_outcome(
    engine: Engine, plan: CalibrationPlan, spec: NullControlSpec, calibration_run_id: str
) -> tuple[NullControlOutcome, str]:
    """Persists one real ``ResearchConclusion`` and one real
    ``PromotionEvaluation`` (``research_calibration_null_outcome.
    conclusion_id`` is a genuine foreign key into ``research_conclusion``)
    and returns the retained ``NullControlOutcome`` plus that conclusion's
    id."""

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
            rationale="Persistence proof null-control conclusion.",
            blockers=("INSUFFICIENT_EFFECTIVE_SAMPLE",),
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
    outcome = NullControlOutcome.create(
        spec_id=spec.spec_id,
        calibration_run_id=calibration_run_id,
        governance_policy_id="research_governance_policy_fixture",
        exploratory_trial_id=None,
        validation_trial_id=None,
        holdout_trial_id=None,
        robustness_run_id=None,
        conclusion_id=conclusion.conclusion_id,
        promotion_evaluation_id=promotion.promotion_evaluation_id,
        knowledge_status=KnowledgeStatus.ACTIVE_RESEARCH,
        blockers=("HOLDOUT_SAMPLE_INSUFFICIENT",),
        created_at=NOW,
    )
    return outcome, conclusion.conclusion_id


def test_null_control_outcome_round_trips_and_lists_by_run(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    spec = repository.save_spec(_spec(plan))
    run = repository.save_run(
        CalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    )
    outcome, _conclusion_id = _governed_null_outcome(
        postgres_engine, plan, spec, run.calibration_run_id
    )
    saved = repository.save_outcome(outcome)
    idempotent = repository.save_outcome(outcome)
    assert saved.outcome_id == idempotent.outcome_id == outcome.outcome_id
    fresh = PostgresCalibrationRepository(postgres_engine)
    assert fresh.outcome_by_spec(spec.spec_id) == outcome
    assert list(fresh.outcomes_for_run(run.calibration_run_id)) == [outcome]


def test_breakdown_method_comparison_and_report_round_trip(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    spec = repository.save_spec(_spec(plan))
    run = repository.save_run(
        CalibrationRun.draft(calibration=plan, spec_ids=(spec.spec_id,), created_at=NOW)
    )
    summary = _rate_summary()
    breakdown = repository.save_breakdown(
        CalibrationBreakdown.create(
            calibration_id=plan.calibration_id,
            dimension_kind="FAMILY",
            dimension_label=NullControlFamily.DETERMINISTIC_PERMUTATION.value,
            control_count=50,
            summary=summary,
            note="Adequate control count for family-specific calibration.",
            created_at=NOW,
        )
    )
    comparison = repository.save_method_comparison(
        StatisticalMethodComparison.create(
            calibration_id=plan.calibration_id,
            method_id="BENJAMINI_HOCHBERG_FDR_V1",
            survivors_on_nulls=0,
            control_count=50,
            conservatism_rank=1,
            notes="BH-FDR, false discovery rate 0.10.",
            created_at=NOW,
        )
    )
    report = repository.save_report(
        CalibrationReport.create(
            calibration_id=plan.calibration_id,
            calibration_run_id=run.calibration_run_id,
            overall_summary=summary,
            family_breakdown_ids=(breakdown.breakdown_id,),
            horizon_breakdown_ids=(),
            time_period_breakdown_ids=(),
            volatility_regime_breakdown_ids=(),
            method_comparison_ids=(comparison.comparison_id,),
            effect_size_floor_policy_id="EFFECT_SIZE_FLOOR_POLICY_V1",
            evidence_grade_policy_id="EVIDENCE_GRADE_POLICY_V1",
            recommended_evidence_policy_notes="Corrected false-positive rate is zero this cycle.",
            prior_agenda_hypotheses_recalibrated=0,
            justified_follow_up_count=0,
            limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
            created_at=NOW,
        )
    )
    idempotent_report = repository.save_report(report)
    assert idempotent_report.report_id == report.report_id

    fresh = PostgresCalibrationRepository(postgres_engine)
    assert breakdown in fresh.breakdowns_for_calibration(plan.calibration_id)
    assert comparison in fresh.method_comparisons_for_calibration(plan.calibration_id)
    assert fresh.report_by_calibration(plan.calibration_id) == report


def test_prior_recalibration_follow_up_priority_and_calibrated_promotion_round_trip(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    calibration_repository = PostgresCalibrationRepository(postgres_engine)
    plan = calibration_repository.save_plan(
        _plan(agenda_id, snapshot_id, replay_run_id, dataset_id)
    )

    from apex.adapters.cross_market_research_postgres import PostgresCrossMarketResearchRepository

    cross_market_repository = PostgresCrossMarketResearchRepository(postgres_engine)
    agenda = cross_market_repository.agenda_by_id(agenda_id)
    hypothesis = cross_market_repository.save_hypothesis(_hypothesis(agenda))
    conclusion = _real_conclusion(
        postgres_engine,
        hypothesis,
        replay_run_id,
        snapshot_id,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        rationale="Prior-agenda recalibration proof conclusion.",
    )

    recalibration = calibration_repository.save_recalibration(
        PriorHypothesisRecalibration.create(
            calibration_id=plan.calibration_id,
            hypothesis_id=hypothesis.hypothesis_id,
            original_conclusion_id=conclusion.conclusion_id,
            original_conclusion_state=ResearchConclusionState.WEAK_SUPPORT.value,
            original_knowledge_status="WEAK",
            calibrated_evidence_grade=EvidenceGrade.EXPLORATORY,
            null_percentile=Decimal("0.24038462"),
            still_promotion_ineligible=True,
            deserves_follow_up=False,
            follow_up_priority=FollowUpPriorityClass.UNSUPPORTED_AFTER_CALIBRATION,
            negative_control_verdict=None,
            rationale="Below the suggestive-floor null percentile.",
            created_at=NOW,
        )
    )
    idempotent_recalibration = calibration_repository.save_recalibration(recalibration)
    assert idempotent_recalibration.recalibration_id == recalibration.recalibration_id

    # Migration 0082's follow-up-priority table has a genuine foreign key
    # into ``cross_market_follow_up_proposal`` -- persist one real row there
    # rather than a fabricated id.
    from apex.domain.cross_market_research import (
        FollowUpKind,
        FollowUpProposal,
        HypothesisTemplateId,
    )

    follow_up = cross_market_repository.save_follow_up(
        FollowUpProposal.create(
            parent_conclusion_id=conclusion.conclusion_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            kind=FollowUpKind.INVESTIGATE_ROBUSTNESS_FAILURE,
            proposed_template_id=HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
            description="Persistence proof follow-up.",
            created_at=NOW,
        )
    )
    assessment = calibration_repository.save_follow_up_priority(
        FollowUpPriorityAssessment.create(
            calibration_id=plan.calibration_id,
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            classification=FollowUpPriorityClass.REJECTED,
            rationale="Parent conclusion did not clear the calibrated evidence bar.",
            created_at=NOW,
        )
    )

    methodology_repository = PostgresResearchMethodologyRepository(postgres_engine)
    base_promotion = methodology_repository.save_promotion_evaluation(
        PromotionEvaluation.create(
            conclusion=conclusion,
            policy=APEX_PRODUCTION_PROMOTION_POLICY,
            holdout_exposed=False,
            robustness_completed=True,
            lookahead_violation=False,
            created_at=NOW,
        )
    )
    calibrated = calibration_repository.save_calibrated_promotion(
        CalibratedPromotionEvaluation.create(
            promotion_evaluation_id=base_promotion.promotion_evaluation_id,
            calibration_id=plan.calibration_id,
            base_eligible=base_promotion.eligible,
            base_reasons=base_promotion.reasons,
            evidence_grade=EvidenceGrade.EXPLORATORY,
            policy=CALIBRATED_PROMOTION_POLICY,
            created_at=NOW,
        )
    )

    fresh = PostgresCalibrationRepository(postgres_engine)
    assert recalibration in fresh.recalibrations_for_calibration(plan.calibration_id)
    assert assessment in fresh.follow_up_priorities_for_calibration(plan.calibration_id)
    assert calibrated in fresh.calibrated_promotions_for_calibration(plan.calibration_id)
    assert calibrated.eligible is False  # EXPLORATORY never clears the VALIDATED minimum


def test_research_calibration_immutable_rows_reject_update_and_delete(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    plan = repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    repository.save_spec(_spec(plan))

    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE research_calibration_plan SET version = 'HACKED' "
                    "WHERE calibration_id = :calibration_id"
                ),
                {"calibration_id": plan.calibration_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM research_calibration_plan"))
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM research_calibration_null_control"))
        connection.rollback()


def _insert_raw_plan(
    connection: sa.Connection,
    *,
    calibration_id: str,
    snapshot_id: str,
    replay_run_id: str,
    dataset_id: str,
    agenda_id: str,
    negative_control_budget: int,
    authority: str,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO research_calibration_plan (calibration_id, version, "
            "dataset_snapshot_id, replay_run_id, cross_market_dataset_id, "
            "source_agenda_id, negative_control_budget, registered_at, payload, authority) "
            "VALUES (:calibration_id, 'V1', :snap, :replay, :dataset, :agenda, "
            ":budget, :now, '{}', :authority)"
        ),
        {
            "calibration_id": calibration_id,
            "snap": snapshot_id,
            "replay": replay_run_id,
            "dataset": dataset_id,
            "agenda": agenda_id,
            "budget": negative_control_budget,
            "now": NOW,
            "authority": authority,
        },
    )


def test_research_calibration_authority_is_always_none(postgres_engine: Engine) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    with postgres_engine.connect() as connection, pytest.raises(Exception) as error:
        _insert_raw_plan(
            connection,
            calibration_id="bad-authority",
            snapshot_id=snapshot_id,
            replay_run_id=replay_run_id,
            dataset_id=dataset_id,
            agenda_id=agenda_id,
            negative_control_budget=60,
            authority="FULL",
        )
    assert "ck_rcp_authority" in str(error.value) or "violates check constraint" in str(error.value)


def test_negative_control_budget_check_constraint_is_enforced_at_the_database_boundary(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    with postgres_engine.connect() as connection, pytest.raises(Exception) as error:
        _insert_raw_plan(
            connection,
            calibration_id="bad-budget",
            snapshot_id=snapshot_id,
            replay_run_id=replay_run_id,
            dataset_id=dataset_id,
            agenda_id=agenda_id,
            negative_control_budget=5,
            authority="NONE",
        )
    assert "ck_rcp_budget" in str(error.value) or "violates check constraint" in str(error.value)


def test_retained_research_calibration_evidence_blocks_destructive_0082_downgrade(
    postgres_engine: Engine,
) -> None:
    agenda_id, snapshot_id, replay_run_id, dataset_id = _seeded_agenda(postgres_engine)
    repository = PostgresCalibrationRepository(postgres_engine)
    repository.save_plan(_plan(agenda_id, snapshot_id, replay_run_id, dataset_id))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0082 with retained research-signal-calibration evidence",
        ):
            command.downgrade(_config(connection), "0081_cross_market_research")
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0082_research_signal_calibration"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0082_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_CALIBRATION_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0081_cross_market_research")
    assert set(RESEARCH_CALIBRATION_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_CALIBRATION_TABLES)
