"""Orchestration-level proofs for Research Signal Calibration and
Negative-Control Hardening v1, against in-memory fakes and the identical
genuine (non-network) fixture panel the cross-market-research application
tests already build -- no database, no provider request.

Covers: deterministic null-control-battery generation, running the full
battery through the exact governed methodology stack (BH-FDR/Bonferroni/
HAC/bootstrap comparison, family/horizon/time-period/volatility-regime
breakdowns), and the non-destructive recalibration of a genuine completed
agenda's already-retained conclusions and follow-up proposals.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apex.application.cross_market_research import (
    AdvanceAgendaRun,
    InMemoryCrossMarketResearchRepository,
    RegisterAgendaRun,
    build_agenda_timeline,
    generate_v1_agenda_hypotheses,
)
from apex.application.research_calibration import (
    V1_CALIBRATION_TOTAL_CONTROLS,
    AdvanceCalibrationRun,
    AssessFollowUpPriority,
    CalibrationApplicationError,
    EvaluateCalibratedPromotion,
    InMemoryCalibrationRepository,
    RecalibratePriorAgenda,
    RegisterCalibrationRun,
    build_v1_calibration_plan,
    derive_null_series,
    generate_v1_null_control_specs,
    median_split_dates,
)
from apex.application.research_methodology import InMemoryResearchMethodologyRepository
from apex.domain.cross_market_research import AgendaRunStatus, FeatureId, OutcomeId
from apex.domain.research_calibration import (
    CalibrationRunStatus,
    EvidenceGrade,
    FollowUpPriorityClass,
    NullControlFamily,
    NullGenerationMethod,
)

# Reuse the exact same genuine (fixture-data, no-network) panel and agenda
# fixtures the cross-market-research application tests already build --
# this milestone's calibration is required to run against the *same*
# dataset/replay lineage, never a refreshed or different one. Imported
# under an alias and re-exposed by thin wrapper fixtures below (rather
# than imported directly under their own names) purely so every test
# function in *this* file can still request them as plain ``panel``/
# ``agenda`` parameters without ruff reading that as shadowing the import.
# Each is genuinely used -- only invisibly to static analysis, through
# pytest's fixture-dependency injection by parameter name below.
from tests.test_cross_market_research_application import agenda as _shared_agenda  # noqa: F401
from tests.test_cross_market_research_application import panel as _shared_panel  # noqa: F401
from tests.test_cross_market_research_application import (  # noqa: F401
    panel_inputs as _shared_panel_inputs,
)

NOW = datetime(2026, 8, 6, tzinfo=UTC)


@pytest.fixture(scope="module")
def panel_inputs(_shared_panel_inputs):  # noqa: F811
    return _shared_panel_inputs


@pytest.fixture(scope="module")
def panel(_shared_panel):  # noqa: F811
    return _shared_panel


@pytest.fixture(scope="module")
def agenda(_shared_agenda):  # noqa: F811
    return _shared_agenda


@pytest.fixture(scope="module")
def calibration_plan(agenda):
    return build_v1_calibration_plan(
        dataset_snapshot_id=agenda.dataset_snapshot_id,
        replay_run_id=agenda.replay_run_id,
        cross_market_dataset_id=agenda.cross_market_dataset_id,
        source_agenda_id=agenda.agenda_id,
        registered_at=NOW,
    )


@pytest.fixture(scope="module")
def completed_calibration_run(calibration_plan, panel):
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    repository = InMemoryCalibrationRepository()
    methodology_repository = InMemoryResearchMethodologyRepository()
    repository.save_plan(calibration_plan)
    for spec in specs:
        repository.save_spec(spec)
    RegisterCalibrationRun(repository=repository).execute(
        calibration=calibration_plan, specs=specs, created_at=NOW
    )
    train_end = panel.session_dates[540]
    validation_end = panel.session_dates[720]
    timeline = build_agenda_timeline(
        panel=panel,
        agenda=_FakeAgendaForTimeline(calibration_plan, panel),
        train_end=train_end,
        validation_end=validation_end,
        created_at=NOW,
    )
    advancer = AdvanceCalibrationRun(
        repository=repository, methodology_repository=methodology_repository, timeline=timeline
    )
    run = None
    for _ in range(len(specs) // 20 + 2):
        run = advancer.execute(
            calibration=calibration_plan, panel=panel, maximum_specs=20, created_at=NOW
        )
        if run.status is CalibrationRunStatus.COMPLETE:
            break
    assert run is not None and run.status is CalibrationRunStatus.COMPLETE
    return run, repository, methodology_repository, specs


class _FakeAgendaForTimeline:
    """``build_agenda_timeline`` only reads ``replay_run_id`` off its
    ``agenda`` argument -- this stands in for the genuine agenda so the
    calibration timeline never needs a second, real ``ResearchAgenda``."""

    def __init__(self, calibration_plan, panel) -> None:
        self.replay_run_id = calibration_plan.replay_run_id
        self.dataset_snapshot_id = calibration_plan.dataset_snapshot_id


@pytest.fixture(scope="module")
def completed_agenda_run(agenda, panel):
    hypotheses = generate_v1_agenda_hypotheses(agenda=agenda, created_at=NOW)
    train_end = panel.session_dates[540]
    validation_end = panel.session_dates[720]
    timeline = build_agenda_timeline(
        panel=panel,
        agenda=agenda,
        train_end=train_end,
        validation_end=validation_end,
        created_at=NOW,
    )
    repository = InMemoryCrossMarketResearchRepository()
    methodology_repository = InMemoryResearchMethodologyRepository()
    repository.save_agenda(agenda)
    for hypothesis in hypotheses:
        repository.save_hypothesis(hypothesis)
    RegisterAgendaRun(repository=repository).execute(
        agenda=agenda, hypotheses=hypotheses, created_at=NOW
    )
    advancer = AdvanceAgendaRun(
        repository=repository,
        methodology_repository=methodology_repository,
        timeline=timeline,
        panel_warnings=panel.warnings,
    )
    run = None
    for _ in range(10):
        run = advancer.execute(agenda=agenda, panel=panel, maximum_hypotheses=6, created_at=NOW)
        if run.status is AgendaRunStatus.COMPLETE:
            break
    assert run is not None and run.status is AgendaRunStatus.COMPLETE
    return repository, methodology_repository


# --------------------------------------------------------------------------
# Null-series derivation: reused primitives, real panel data, point-in-time
# safety through the application-layer glue (not just the pure transforms).
# --------------------------------------------------------------------------


def test_median_split_dates_derives_the_threshold_only_from_train_dates(panel) -> None:
    feature_by_date = panel.feature_by_date(FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING)
    train_dates = frozenset(panel.session_dates[:400])
    above_median = median_split_dates(feature_by_date=feature_by_date, train_dates=train_dates)
    # Every date in the whole panel is classified (not only train dates) --
    # the threshold is derived from train, then applied to everyone.
    assert above_median <= frozenset(feature_by_date)
    assert any(d not in train_dates for d in above_median)


def test_derive_null_series_temporal_displacement_never_reads_a_later_date(
    panel, calibration_plan
) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    lag_spec = next(s for s in specs if s.family is NullControlFamily.TEMPORAL_DISPLACEMENT)
    series = derive_null_series(spec=lag_spec, panel=panel)
    lag_sessions = int(dict(lag_spec.method_params)["lag_sessions"])
    real = panel.feature_by_date(FeatureId(lag_spec.source_feature_id))
    ordered_real_dates = [d for d in panel.session_dates if d in real]
    for item in series:
        index = ordered_real_dates.index(item.session_date)
        assert index >= lag_sessions
        assert item.value == real[ordered_real_dates[index - lag_sessions]]


def test_derive_null_series_economic_pairing_reuses_the_real_untransformed_series(
    panel, calibration_plan
) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    economic_spec = next(
        s for s in specs if s.family is NullControlFamily.ECONOMIC_NEGATIVE_CONTROL
    )
    series = derive_null_series(spec=economic_spec, panel=panel)
    real = panel.feature_by_date(FeatureId(economic_spec.source_feature_id))
    for item in series:
        assert item.value == real[item.session_date]


def test_derive_null_series_is_deterministic_across_repeated_calls(panel, calibration_plan) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    for spec in specs[:10]:
        first = derive_null_series(spec=spec, panel=panel)
        second = derive_null_series(spec=spec, panel=panel)
        assert first == second


def test_unconditional_baseline_series_makes_the_sample_equal_the_full_universe(
    panel, calibration_plan
) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    unconditional = next(
        s for s in specs if s.null_method is NullGenerationMethod.UNCONDITIONAL_BASELINE
    )
    series = derive_null_series(spec=unconditional, panel=panel)
    feature_by_date = {item.session_date: item.value for item in series}
    train_dates = frozenset(panel.session_dates[:400])
    primary = median_split_dates(feature_by_date=feature_by_date, train_dates=train_dates)
    assert primary == frozenset(feature_by_date)


# --------------------------------------------------------------------------
# Deterministic, declared-before-results battery generation
# --------------------------------------------------------------------------


def test_v1_battery_size_is_declared_and_within_the_bounded_range(calibration_plan) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    assert len(specs) == V1_CALIBRATION_TOTAL_CONTROLS
    assert 50 <= len(specs) <= 150


def test_v1_battery_generation_is_byte_identical_across_calls(calibration_plan) -> None:
    first = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    second = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    assert [item.spec_id for item in first] == [item.spec_id for item in second]


def test_v1_battery_covers_every_declared_null_control_family(calibration_plan) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    families = {item.family for item in specs}
    assert families == set(NullControlFamily)


def test_v1_battery_covers_both_five_and_twenty_session_horizons(calibration_plan) -> None:
    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    horizons = {item.horizon_sessions for item in specs}
    assert horizons == {5, 20}


def test_v1_battery_never_uses_the_persistence_indicator_outcome(calibration_plan) -> None:
    """This calibration battery's runner only implements the predictive-
    relationship trial shape (a declared limitation) -- confirm no spec
    accidentally references the persistence-only outcome it cannot
    evaluate correctly."""

    specs = generate_v1_null_control_specs(calibration=calibration_plan, created_at=NOW)
    assert all(item.outcome_id != OutcomeId.PERSISTENCE_INDICATOR.value for item in specs)


# --------------------------------------------------------------------------
# Running the battery through the exact governed methodology stack
# --------------------------------------------------------------------------


def test_calibration_run_processes_every_spec_exactly_once(completed_calibration_run) -> None:
    run, _repository, _methodology_repository, specs = completed_calibration_run
    assert run.cursor_ordinal == len(specs)
    assert len(run.completed_spec_ids) == len(specs)
    assert len(set(run.completed_spec_ids)) == len(specs)


def test_every_null_control_outcome_is_retained_with_authority_none(
    completed_calibration_run,
) -> None:
    run, repository, _methodology_repository, specs = completed_calibration_run
    outcomes = repository.outcomes_for_run(run.calibration_run_id)
    assert len(outcomes) == len(specs)
    assert all(item.authority == "NONE" for item in outcomes)


def test_resuming_a_complete_calibration_run_is_a_safe_no_op(completed_calibration_run) -> None:
    run, repository, methodology_repository, _specs = completed_calibration_run
    timeline_advancer = AdvanceCalibrationRun(
        repository=repository,
        methodology_repository=methodology_repository,
        timeline=None,  # type: ignore[arg-type]
    )
    plan = repository.plan_by_id(run.calibration_id)
    replay = timeline_advancer.execute(
        calibration=plan,
        panel=None,
        maximum_specs=10,
        created_at=NOW,  # type: ignore[arg-type]
    )
    assert replay.calibration_run_id == run.calibration_run_id
    assert replay.status is CalibrationRunStatus.COMPLETE


def test_report_is_built_automatically_on_completion(completed_calibration_run) -> None:
    run, repository, _methodology_repository, _specs = completed_calibration_run
    report = repository.report_by_calibration(run.calibration_id)
    assert report is not None
    assert report.calibration_run_id == run.calibration_run_id


def test_overall_summary_counts_are_internally_consistent(completed_calibration_run) -> None:
    run, repository, _methodology_repository, specs = completed_calibration_run
    report = repository.report_by_calibration(run.calibration_id)
    summary = report.overall_summary
    assert summary.total_controls == len(specs)
    assert (
        summary.bh_fdr_survivors <= summary.raw_positive_findings
        or summary.raw_positive_findings == 0
    )
    assert 0 <= summary.weak_count <= summary.total_controls
    assert summary.supported_count <= summary.total_controls
    assert summary.promotion_eligible_count <= summary.total_controls


def test_family_breakdowns_cover_every_family_and_sum_to_the_total(
    completed_calibration_run,
) -> None:
    run, repository, _methodology_repository, specs = completed_calibration_run
    breakdowns = repository.breakdowns_for_calibration(run.calibration_id)
    family_breakdowns = [item for item in breakdowns if item.dimension_kind == "FAMILY"]
    assert {item.dimension_label for item in family_breakdowns} == {
        item.value for item in NullControlFamily
    }
    assert sum(item.control_count for item in family_breakdowns) == len(specs)


def test_horizon_breakdowns_cover_five_and_twenty_sessions(completed_calibration_run) -> None:
    run, repository, _methodology_repository, specs = completed_calibration_run
    breakdowns = repository.breakdowns_for_calibration(run.calibration_id)
    horizon_breakdowns = [item for item in breakdowns if item.dimension_kind == "HORIZON"]
    assert {item.dimension_label for item in horizon_breakdowns} == {"5_SESSIONS", "20_SESSIONS"}
    assert sum(item.control_count for item in horizon_breakdowns) == len(specs)


def test_time_period_and_volatility_regime_breakdowns_are_retained(
    completed_calibration_run,
) -> None:
    run, repository, _methodology_repository, specs = completed_calibration_run
    breakdowns = repository.breakdowns_for_calibration(run.calibration_id)
    time_period = [item for item in breakdowns if item.dimension_kind == "TIME_PERIOD"]
    volatility_regime = [item for item in breakdowns if item.dimension_kind == "VOLATILITY_REGIME"]
    assert {item.dimension_label for item in time_period} >= {"EARLY", "MIDDLE", "LATE"}
    assert sum(item.control_count for item in time_period) == len(specs)
    assert volatility_regime  # at least one volatility-regime bucket was retained
    assert sum(item.control_count for item in volatility_regime) == len(specs)


def test_statistical_method_comparison_covers_every_declared_method(
    completed_calibration_run,
) -> None:
    run, repository, _methodology_repository, _specs = completed_calibration_run
    comparisons = repository.method_comparisons_for_calibration(run.calibration_id)
    method_ids = {item.method_id for item in comparisons}
    assert method_ids == {
        "NORMAL_APPROXIMATION_UNCORRECTED_V1",
        "HAC_NEWEY_WEST_UNCORRECTED_V1",
        "SEEDED_BOOTSTRAP_UNCORRECTED_V1",
        "BENJAMINI_HOCHBERG_FDR_V1",
        "BONFERRONI_V1",
    }
    ranks = sorted(item.conservatism_rank for item in comparisons)
    assert ranks == list(range(1, len(comparisons) + 1))


def test_bonferroni_is_at_least_as_conservative_as_benjamini_hochberg(
    completed_calibration_run,
) -> None:
    """Bonferroni's family-wise correction is provably at least as strict as
    BH-FDR at the same nominal level -- confirm the calibration's own
    empirical comparison reflects that ordering, not just assert it in the
    abstract."""

    run, repository, _methodology_repository, _specs = completed_calibration_run
    comparisons = {
        item.method_id: item
        for item in repository.method_comparisons_for_calibration(run.calibration_id)
    }
    assert (
        comparisons["BONFERRONI_V1"].survivors_on_nulls
        <= comparisons["BENJAMINI_HOCHBERG_FDR_V1"].survivors_on_nulls
    )


def test_calibration_report_discloses_its_declared_limitations(completed_calibration_run) -> None:
    run, repository, _methodology_repository, _specs = completed_calibration_run
    report = repository.report_by_calibration(run.calibration_id)
    assert "ZERO_INTERACTION_CONDITIONAL_MIMICKING_NULL_CONTROLS_THIS_CYCLE" in report.limitations
    assert (
        "NULL_CONTROLS_USE_ONLY_THE_PREDICTIVE_RELATIONSHIP_TRIAL_SHAPE_NOT_PERSISTENCE_OR_CONDITIONAL"
        in report.limitations
    )


def test_no_strategy_opportunity_paper_or_live_authority_anywhere_in_calibration_evidence(
    completed_calibration_run,
) -> None:
    run, repository, _methodology_repository, _specs = completed_calibration_run
    plan = repository.plan_by_id(run.calibration_id)
    assert plan.authority == "NONE"
    assert run.authority == "NONE"
    for outcome in repository.outcomes_for_run(run.calibration_run_id):
        assert outcome.authority == "NONE"
    for breakdown in repository.breakdowns_for_calibration(run.calibration_id):
        assert breakdown.authority == "NONE"
    report = repository.report_by_calibration(run.calibration_id)
    assert report.authority == "NONE"


# --------------------------------------------------------------------------
# Prior-agenda recalibration and follow-up priority -- non-destructive
# --------------------------------------------------------------------------


def test_recalibration_never_touches_the_original_retained_knowledge_entries(
    completed_agenda_run, completed_calibration_run
) -> None:
    agenda_repository, methodology_repository = completed_agenda_run
    run, calibration_repository, calibration_methodology_repository, _specs = (
        completed_calibration_run
    )
    before = {
        item.entry_id: item.canonical_hash for item in agenda_repository.list_knowledge_entries()
    }

    null_cohens_d = []
    for outcome in calibration_repository.outcomes_for_run(run.calibration_run_id):
        if outcome.validation_trial_id:
            trial = calibration_methodology_repository.trial_by_id(outcome.validation_trial_id)
            if (
                trial
                and trial.diagnostics.effect_size
                and trial.diagnostics.effect_size.cohens_d is not None
            ):
                null_cohens_d.append(trial.diagnostics.effect_size.cohens_d)

    plan = calibration_repository.plan_by_id(run.calibration_id)
    recalibrations = RecalibratePriorAgenda(
        cross_market_repository=agenda_repository, methodology_repository=methodology_repository
    ).execute(
        calibration=plan,
        source_agenda_id=agenda_repository.list_agendas()[0].agenda_id,
        null_cohens_d=tuple(null_cohens_d),
        created_at=NOW,
    )

    after = {
        item.entry_id: item.canonical_hash for item in agenda_repository.list_knowledge_entries()
    }
    assert before == after
    assert before  # the fixture genuinely retained knowledge entries to protect
    assert len(recalibrations) == len(
        agenda_repository.hypotheses_for_agenda(plan.source_agenda_id)
    )


def test_recalibration_produces_one_derived_assessment_per_hypothesis(
    completed_agenda_run, completed_calibration_run
) -> None:
    agenda_repository, methodology_repository = completed_agenda_run
    run, calibration_repository, calibration_methodology_repository, _specs = (
        completed_calibration_run
    )
    null_cohens_d = tuple(
        trial.diagnostics.effect_size.cohens_d
        for outcome in calibration_repository.outcomes_for_run(run.calibration_run_id)
        if outcome.validation_trial_id
        and (trial := calibration_methodology_repository.trial_by_id(outcome.validation_trial_id))
        and trial.diagnostics.effect_size
        and trial.diagnostics.effect_size.cohens_d is not None
    )
    plan = calibration_repository.plan_by_id(run.calibration_id)
    source_agenda = agenda_repository.list_agendas()[0]
    hypotheses = agenda_repository.hypotheses_for_agenda(source_agenda.agenda_id)
    recalibrations = RecalibratePriorAgenda(
        cross_market_repository=agenda_repository, methodology_repository=methodology_repository
    ).execute(
        calibration=plan,
        source_agenda_id=source_agenda.agenda_id,
        null_cohens_d=null_cohens_d,
        created_at=NOW,
    )
    assert {item.hypothesis_id for item in recalibrations} == {
        item.hypothesis_id for item in hypotheses
    }
    for item in recalibrations:
        assert item.authority == "NONE"
        assert item.calibrated_evidence_grade in set(EvidenceGrade)

    # Negative controls (and only negative controls) receive a quality verdict.
    negative_control_ids = {item.hypothesis_id for item in hypotheses if item.is_negative_control}
    for item in recalibrations:
        if item.hypothesis_id in negative_control_ids:
            assert item.negative_control_verdict is not None
        else:
            assert item.negative_control_verdict is None


def test_follow_up_priority_classifies_every_retained_follow_up_proposal(
    completed_agenda_run, completed_calibration_run
) -> None:
    agenda_repository, methodology_repository = completed_agenda_run
    run, calibration_repository, calibration_methodology_repository, _specs = (
        completed_calibration_run
    )
    null_cohens_d = tuple(
        trial.diagnostics.effect_size.cohens_d
        for outcome in calibration_repository.outcomes_for_run(run.calibration_run_id)
        if outcome.validation_trial_id
        and (trial := calibration_methodology_repository.trial_by_id(outcome.validation_trial_id))
        and trial.diagnostics.effect_size
        and trial.diagnostics.effect_size.cohens_d is not None
    )
    plan = calibration_repository.plan_by_id(run.calibration_id)
    source_agenda = agenda_repository.list_agendas()[0]
    recalibrations = RecalibratePriorAgenda(
        cross_market_repository=agenda_repository, methodology_repository=methodology_repository
    ).execute(
        calibration=plan,
        source_agenda_id=source_agenda.agenda_id,
        null_cohens_d=null_cohens_d,
        created_at=NOW,
    )
    follow_ups = agenda_repository.list_follow_ups()
    assessments = AssessFollowUpPriority(cross_market_repository=agenda_repository).execute(
        calibration=plan, recalibrations=recalibrations, created_at=NOW
    )
    assert {item.follow_up_id for item in assessments} == {item.follow_up_id for item in follow_ups}
    assert all(item.classification in set(FollowUpPriorityClass) for item in assessments)
    assert all(item.authority == "NONE" for item in assessments)


# --------------------------------------------------------------------------
# Calibrated production promotion
# --------------------------------------------------------------------------


def test_evaluate_calibrated_promotion_reuses_the_unmodified_base_promotion(
    completed_agenda_run,
) -> None:
    agenda_repository, methodology_repository = completed_agenda_run
    hypothesis = agenda_repository.list_agendas()[0]
    hypotheses = agenda_repository.hypotheses_for_agenda(hypothesis.agenda_id)
    execution = agenda_repository.execution_by_hypothesis(hypotheses[0].hypothesis_id)
    assert execution is not None and execution.conclusion_id is not None
    evaluation = EvaluateCalibratedPromotion(methodology_repository=methodology_repository).execute(
        conclusion_id=execution.conclusion_id,
        calibration_id="test-calibration",
        evidence_grade=EvidenceGrade.STRONG,
        created_at=NOW,
    )
    assert evaluation.authority == "NONE"
    assert evaluation.calibration_id == "test-calibration"
    assert evaluation.promotion_evaluation_id


def test_calibration_application_error_is_the_declared_error_hierarchy() -> None:
    assert issubclass(CalibrationApplicationError, RuntimeError)
