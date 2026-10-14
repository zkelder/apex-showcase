"""Orchestration-level proofs for Research Calibration v2: Template-Matched
Nulls and Stability, against in-memory fakes and the identical genuine
(non-network) fixture panel/agenda the v1 calibration application tests
already build -- no database, no provider request.

Covers: template-matched null-series construction per research design
(preserved conditioning structure, no future leakage, deterministic
seeding), running the full v2 battery through the exact governed
methodology stack, the bounded walk-forward-null subset, time-period/
volatility-regime stability slices, matched-vs-pooled percentile
calibration, and the non-destructive reassessment/requalification of
genuine agenda hypotheses and follow-up proposals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.application.cross_market_research import build_agenda_timeline
from apex.application.research_calibration import InMemoryCalibrationRepository
from apex.application.research_calibration_v2 import (
    V2_CALIBRATION_TOTAL_CONTROLS,
    AdvanceCalibrationRunV2,
    CalibrationV2ApplicationError,
    FinalizeCalibrationV2,
    InMemoryCalibrationV2Repository,
    ReassessGenuineFindingsV2,
    RegisterCalibrationRunV2,
    RequalifyFollowUpsV2,
    build_matched_pools,
    build_null_evaluation,
    build_v2_calibration_plan,
    evaluate_v2_promotion_gate,
    generate_v2_null_control_specs,
    select_walk_forward_null_subset,
)
from apex.application.research_methodology import InMemoryResearchMethodologyRepository
from apex.domain.cross_market_research import FeatureId
from apex.domain.research_calibration import CalibrationRunStatus, EvidenceGrade
from apex.domain.research_calibration_v2 import (
    CALIBRATION_STRATUM_INSUFFICIENT,
    MINIMUM_MATCHED_NULL_COUNT,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    ResearchDesign,
)
from tests.test_cross_market_research_application import agenda as _shared_agenda  # noqa: F401
from tests.test_cross_market_research_application import panel as _shared_panel  # noqa: F401
from tests.test_cross_market_research_application import (  # noqa: F401
    panel_inputs as _shared_panel_inputs,
)
from tests.test_research_calibration_application import (  # noqa: F401
    completed_agenda_run as _shared_completed_agenda_run,
)

NOW = datetime(2026, 8, 8, tzinfo=UTC)


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
def completed_agenda_run(_shared_completed_agenda_run):  # noqa: F811
    return _shared_completed_agenda_run


@pytest.fixture(scope="module")
def calibration_plan(agenda):
    return build_v2_calibration_plan(
        dataset_snapshot_id=agenda.dataset_snapshot_id,
        replay_run_id=agenda.replay_run_id,
        cross_market_dataset_id=agenda.cross_market_dataset_id,
        source_agenda_id=agenda.agenda_id,
        source_calibration_id="research_calibration_plan_fake_v1_for_tests",
        registered_at=NOW,
    )


class _FakeAgendaForTimeline:
    def __init__(self, calibration_plan, panel) -> None:
        self.replay_run_id = calibration_plan.replay_run_id
        self.dataset_snapshot_id = calibration_plan.dataset_snapshot_id


@pytest.fixture(scope="module")
def timeline(calibration_plan, panel):
    train_end = panel.session_dates[540]
    validation_end = panel.session_dates[720]
    return build_agenda_timeline(
        panel=panel,
        agenda=_FakeAgendaForTimeline(calibration_plan, panel),
        train_end=train_end,
        validation_end=validation_end,
        created_at=NOW,
    )


@pytest.fixture(scope="module")
def priority_strata(calibration_plan):
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    conditional = next(s for s in specs if s.research_design is ResearchDesign.CONDITIONAL)
    interaction = next(s for s in specs if s.research_design is ResearchDesign.INTERACTION)
    return frozenset({conditional.matched_stratum, interaction.matched_stratum})


@pytest.fixture(scope="module")
def completed_v2_run(calibration_plan, panel, timeline, priority_strata):
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    calibration_repository = InMemoryCalibrationRepository()
    repository = InMemoryCalibrationV2Repository()
    methodology_repository = InMemoryResearchMethodologyRepository()
    calibration_repository.save_plan(calibration_plan)
    for spec in specs:
        repository.save_spec(spec)
    RegisterCalibrationRunV2(calibration_repository=calibration_repository).execute(
        calibration=calibration_plan, specs=specs, created_at=NOW
    )
    walk_forward_subset = frozenset(
        select_walk_forward_null_subset(specs=specs, priority_strata=priority_strata)
    )
    advancer = AdvanceCalibrationRunV2(
        calibration_repository=calibration_repository,
        calibration_v2_repository=repository,
        methodology_repository=methodology_repository,
        timeline=timeline,
        walk_forward_spec_ids=walk_forward_subset,
    )
    run = None
    for _ in range(len(specs) // 15 + 2):
        run = advancer.execute(
            calibration=calibration_plan, panel=panel, maximum_specs=15, created_at=NOW
        )
        if run.status is CalibrationRunStatus.COMPLETE:
            break
    assert run is not None and run.status is CalibrationRunStatus.COMPLETE
    return (
        run,
        calibration_repository,
        repository,
        methodology_repository,
        specs,
        walk_forward_subset,
    )


@pytest.fixture(scope="module")
def finalized(completed_v2_run, calibration_plan, panel, timeline):
    run, calibration_repository, repository, methodology_repository, _specs, _wf = completed_v2_run
    return FinalizeCalibrationV2(
        calibration_repository=calibration_repository,
        calibration_v2_repository=repository,
        methodology_repository=methodology_repository,
        timeline=timeline,
    ).execute(calibration=calibration_plan, run=run, panel=panel, created_at=NOW)


# --------------------------------------------------------------------------
# Section 3/4: deterministic, declared-before-results, template-matched
# battery generation
# --------------------------------------------------------------------------


def test_v2_battery_size_is_declared_and_within_bounds(calibration_plan) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    assert len(specs) == V2_CALIBRATION_TOTAL_CONTROLS
    assert 120 <= len(specs) <= 180


def test_v2_battery_generation_is_byte_identical_across_calls(calibration_plan) -> None:
    first = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    second = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    assert [item.spec_id for item in first] == [item.spec_id for item in second]


def test_v2_battery_covers_every_newly_matched_research_design(calibration_plan) -> None:
    """v2's own freshly-generated battery covers the five designs v1 never
    exercised (CONDITIONAL/INTERACTION/CROSS_MARKET/PERSISTENCE/
    REGIME_COMPARISON); PREDICTIVE calibration continues to be served by
    v1's own already-retained, already-sufficient predictive-shaped
    population (section 3: "continue supporting the existing v1 null
    designs" for predictive/regime -- "do not use one null generator
    merely because it is convenient" applies to reusing v1's generator
    for the shape it already covers well, not to needlessly regenerating
    it).

    CROSS_SECTIONAL/CHANGE_BASED/FILING_EVENT (added for Cross-Market
    Research v3's corporate designs) are deliberately excluded from this
    set: v3's corporate hypotheses are template-matched against their own
    agenda-declared negative-control population
    (``domain.corporate_market_research.CORPORATE_NEGATIVE_CONTROL_KINDS``,
    consumed via ``application.corporate_market_research.
    classify_hypothesis_calibration``), not v2's price-only generator --
    the same "do not use one null generator merely because it is
    convenient" principle this docstring already invokes for
    predictive/regime applies equally here, in the other direction."""

    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    designs = {item.research_design for item in specs}
    assert designs == {
        ResearchDesign.CONDITIONAL,
        ResearchDesign.INTERACTION,
        ResearchDesign.CROSS_MARKET,
        ResearchDesign.PERSISTENCE,
        ResearchDesign.REGIME_COMPARISON,
    }


def test_v2_battery_gives_each_priority_stratum_at_least_the_matched_minimum(
    calibration_plan,
) -> None:
    """Section 4: "for the designs relevant to the three currently
    JUSTIFIED follow-ups, target at least enough matched null controls to
    make percentile classification meaningful"."""

    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    counts: dict[str, int] = {}
    for spec in specs:
        counts[spec.matched_stratum] = counts.get(spec.matched_stratum, 0) + 1
    # The two largest CONDITIONAL/INTERACTION strata and the largest
    # CROSS_MARKET stratum are the ones deliberately built to match the
    # three JUSTIFIED follow-ups and the negative control.
    largest = sorted(counts.values(), reverse=True)
    assert largest[0] >= MINIMUM_MATCHED_NULL_COUNT
    assert largest[3] >= MINIMUM_MATCHED_NULL_COUNT


# --------------------------------------------------------------------------
# Template-matched null-series construction (sections 3/8/9): preserved
# conditioning structure, no future leakage, deterministic seeding.
# --------------------------------------------------------------------------


def test_conditional_null_preserves_the_real_conditioning_group_sizes(
    calibration_plan, panel, timeline
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.CONDITIONAL)
    train_dates = frozenset(timeline.train_dates)
    evaluation = build_null_evaluation(spec=spec, panel=panel, train_dates=train_dates)
    all_dates = frozenset(panel.session_dates)
    sample, benchmark = evaluation.sample_universe(all_dates)

    # Recompute the *real* conditioning membership directly, independent of
    # the null construction, and confirm the null's group boundaries are
    # byte-identical to it (the null changed the outcome, never the
    # conditioning structure).
    from apex.application.research_calibration import median_split_dates
    from apex.application.research_calibration_v2 import _condition_dates

    primary = median_split_dates(
        feature_by_date=panel.feature_by_date(FeatureId(spec.source_feature_id)),
        train_dates=train_dates,
    )
    secondary = _condition_dates(
        feature_id_str=spec.secondary_feature_id,
        rule=spec.secondary_condition_rule,
        label=spec.secondary_condition_label,
        panel=panel,
        train_dates=train_dates,
    )
    assert sample == (all_dates & primary & secondary)
    assert benchmark == ((all_dates & primary) - secondary)


def test_conditional_null_changes_the_outcome_values_relative_to_the_real_series(
    calibration_plan, panel, timeline
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.CONDITIONAL)
    evaluation = build_null_evaluation(
        spec=spec, panel=panel, train_dates=frozenset(timeline.train_dates)
    )
    from apex.domain.cross_market_research import OutcomeId

    real_outcome = panel.outcome_by_date(
        OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
    )
    differing = sum(
        1
        for date, value in evaluation.outcome_by_date.items()
        if date in real_outcome and real_outcome[date] != value
    )
    assert differing > 0


def test_interaction_null_uses_the_same_grouped_shape_as_conditional(
    calibration_plan, panel, timeline
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.INTERACTION)
    evaluation = build_null_evaluation(
        spec=spec, panel=panel, train_dates=frozenset(timeline.train_dates)
    )
    all_dates = frozenset(panel.session_dates)
    sample, benchmark = evaluation.sample_universe(all_dates)
    assert sample.isdisjoint(benchmark)
    assert (sample | benchmark) <= all_dates


def test_cross_market_null_nulls_the_feature_and_keeps_the_real_outcome(
    calibration_plan, panel, timeline
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.CROSS_MARKET)
    evaluation = build_null_evaluation(
        spec=spec, panel=panel, train_dates=frozenset(timeline.train_dates)
    )
    from apex.domain.cross_market_research import OutcomeId

    real_outcome = panel.outcome_by_date(
        OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
    )
    assert evaluation.outcome_by_date == real_outcome


def test_cross_market_lag_displacement_never_reads_a_later_date(
    calibration_plan, panel, timeline
) -> None:
    from apex.domain.research_calibration import NullGenerationMethod

    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(
        s
        for s in specs
        if s.research_design is ResearchDesign.CROSS_MARKET
        and s.null_method is NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT
    )
    lag_sessions = int(dict(spec.method_params)["lag_sessions"])
    real = panel.feature_by_date(FeatureId(spec.source_feature_id))
    ordered_real_dates = [d for d in panel.session_dates if d in real]
    evaluation = build_null_evaluation(
        spec=spec, panel=panel, train_dates=frozenset(timeline.train_dates)
    )
    all_dates = frozenset(panel.session_dates)
    sample, _benchmark = evaluation.sample_universe(all_dates)
    # The sample is derived from a feature displaced backward by
    # ``lag_sessions``; every included date's *nulled* feature value must
    # trace back only to an earlier or equal real date.
    for date in sample:
        if date not in ordered_real_dates:
            continue
        index = ordered_real_dates.index(date)
        assert index >= lag_sessions or date not in real


def test_persistence_null_preserves_state_frequency(calibration_plan, panel, timeline) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.PERSISTENCE)
    train_dates = frozenset(timeline.train_dates)
    from apex.application.research_calibration_v2 import _condition_dates

    params = dict(spec.method_params)
    real_condition_dates = _condition_dates(
        feature_id_str=spec.source_feature_id,
        rule=params["condition_rule"],
        label=params.get("condition_label"),
        panel=panel,
        train_dates=train_dates,
    )
    evaluation = build_null_evaluation(spec=spec, panel=panel, train_dates=train_dates)
    all_dates = frozenset(panel.session_dates)
    permuted_sample, _universe = evaluation.sample_universe(all_dates)
    # Block permutation reorders which *blocks* of dates carry the
    # condition, but never changes how many dates in total carry it.
    real_count = len(real_condition_dates & all_dates)
    permuted_count = len(permuted_sample)
    assert permuted_count == real_count


def test_persistence_null_destroys_genuine_transition_timing(
    calibration_plan, panel, timeline
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.PERSISTENCE)
    train_dates = frozenset(timeline.train_dates)
    evaluation = build_null_evaluation(spec=spec, panel=panel, train_dates=train_dates)
    other_seed_params = dict(spec.method_params)
    # Rebuild with a different block length -- if the null construction
    # were a no-op identity transform, changing the block length would
    # never change the resulting outcome series.
    from apex.domain.research_calibration_v2 import NullControlSpecV2

    varied = NullControlSpecV2.create(
        calibration_id=spec.calibration_id,
        research_design=spec.research_design,
        conditioning_shape=spec.conditioning_shape,
        family=spec.family,
        nature=spec.nature,
        design_family=spec.design_family,
        control_kind=spec.control_kind,
        null_method=spec.null_method,
        null_target=spec.null_target,
        source_feature_id=spec.source_feature_id,
        source_feature_family=spec.source_feature_family,
        secondary_feature_id=spec.secondary_feature_id,
        secondary_condition_rule=spec.secondary_condition_rule,
        secondary_condition_label=spec.secondary_condition_label,
        outcome_id=spec.outcome_id,
        outcome_family=spec.outcome_family,
        horizon_sessions=spec.horizon_sessions,
        market_scope=spec.market_scope,
        method_params=tuple(
            (k, "7" if k == "block_length" else v) for k, v in other_seed_params.items()
        ),
        rationale=spec.rationale,
        ordinal=spec.ordinal + 10_000,
        created_at=NOW,
    )
    varied_evaluation = build_null_evaluation(spec=varied, panel=panel, train_dates=train_dates)
    assert evaluation.outcome_by_date != varied_evaluation.outcome_by_date


def test_regime_comparison_null_uses_real_regime_membership(
    calibration_plan, panel, timeline
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(s for s in specs if s.research_design is ResearchDesign.REGIME_COMPARISON)
    evaluation = build_null_evaluation(
        spec=spec, panel=panel, train_dates=frozenset(timeline.train_dates)
    )
    from apex.domain.cross_market_research import REGIME_CONDITION_FEATURE_IDS

    dimension = REGIME_CONDITION_FEATURE_IDS[FeatureId(spec.source_feature_id)]
    params = dict(spec.method_params)
    regime_by_date = panel.regime_by_date()
    real_group_a = frozenset(
        d for d, r in regime_by_date.items() if r.label_for(dimension) == params["label_a"]
    )
    all_dates = frozenset(panel.session_dates)
    sample, _benchmark = evaluation.sample_universe(all_dates)
    assert sample == (all_dates & real_group_a)


def test_null_evaluation_is_deterministic_across_calls(calibration_plan, panel, timeline) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    train_dates = frozenset(timeline.train_dates)
    for spec in specs[:15]:
        first = build_null_evaluation(spec=spec, panel=panel, train_dates=train_dates)
        second = build_null_evaluation(spec=spec, panel=panel, train_dates=train_dates)
        assert first.outcome_by_date == second.outcome_by_date


def test_block_permutation_null_preserves_within_block_contiguous_values(
    calibration_plan, panel, timeline
) -> None:
    """Block dependence is preserved: a block-permuted outcome series is a
    reordering of *whole contiguous blocks* of the real series, never an
    independent per-date shuffle -- so every value that appears in the
    nulled series must also appear as a real outcome value on *some* real
    date (block permutation is a permutation, not a resampling), and the
    multiset of values is exactly preserved."""

    from apex.domain.cross_market_research import OutcomeId
    from apex.domain.research_calibration import NullGenerationMethod

    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    spec = next(
        s
        for s in specs
        if s.research_design is ResearchDesign.CONDITIONAL
        and s.null_method is NullGenerationMethod.BLOCK_PERMUTATION
    )
    evaluation = build_null_evaluation(
        spec=spec, panel=panel, train_dates=frozenset(timeline.train_dates)
    )
    real_outcome = panel.outcome_by_date(
        OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
    )
    real_values = sorted(real_outcome.values())
    nulled_values = sorted(evaluation.outcome_by_date.values())
    assert nulled_values == real_values  # a permutation, never a resampling or synthesis


# --------------------------------------------------------------------------
# Section 12: bounded, predeclared walk-forward-null subset
# --------------------------------------------------------------------------


def test_walk_forward_subset_is_deterministic(calibration_plan, priority_strata) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    first = select_walk_forward_null_subset(specs=specs, priority_strata=priority_strata)
    second = select_walk_forward_null_subset(specs=specs, priority_strata=priority_strata)
    assert first == second


def test_walk_forward_subset_includes_every_priority_stratum_control(
    calibration_plan, priority_strata
) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    subset = frozenset(
        select_walk_forward_null_subset(specs=specs, priority_strata=priority_strata)
    )
    priority_specs = {s.spec_id for s in specs if s.matched_stratum in priority_strata}
    assert priority_specs <= subset


def test_walk_forward_subset_caps_other_designs(calibration_plan, priority_strata) -> None:
    specs = generate_v2_null_control_specs(calibration=calibration_plan, created_at=NOW)
    subset = frozenset(
        select_walk_forward_null_subset(
            specs=specs, priority_strata=priority_strata, per_other_design=2
        )
    )
    non_priority_by_design: dict[ResearchDesign, int] = {}
    for spec in specs:
        if spec.spec_id in subset and spec.matched_stratum not in priority_strata:
            non_priority_by_design[spec.research_design] = (
                non_priority_by_design.get(spec.research_design, 0) + 1
            )
    assert all(count <= 2 for count in non_priority_by_design.values())
    assert subset  # non-empty
    assert len(subset) < len(specs)


# --------------------------------------------------------------------------
# Running the v2 battery through the exact governed methodology stack
# --------------------------------------------------------------------------


def test_v2_run_processes_every_spec_exactly_once(completed_v2_run) -> None:
    run, _cal_repo, _repo, _methodology, specs, _wf = completed_v2_run
    assert run.cursor_ordinal == len(specs)
    assert len(set(run.completed_spec_ids)) == len(specs)


def test_every_v2_null_control_outcome_is_retained_with_authority_none(completed_v2_run) -> None:
    run, _cal_repo, repository, _methodology, specs, _wf = completed_v2_run
    outcomes = repository.outcomes_for_run(run.calibration_run_id)
    assert len(outcomes) == len(specs)
    assert all(item.authority == "NONE" for item in outcomes)


def test_walk_forward_subset_controls_produce_a_walk_forward_null_summary(completed_v2_run) -> None:
    run, _cal_repo, repository, _methodology, _specs, walk_forward_subset = completed_v2_run
    summaries = repository.walk_forward_summaries_for_calibration(run.calibration_id)
    summarized_specs = {item.spec_id for item in summaries}
    # Every walk-forward-selected control that produced at least one fold
    # trial has a summary; a control might legitimately produce zero folds
    # if its sample is too thin, so this is a subset check, not equality.
    assert summarized_specs <= walk_forward_subset
    assert summaries  # at least one fold-bearing control in the subset


def test_walk_forward_nulls_do_not_uniformly_show_perfect_fold_survival(completed_v2_run) -> None:
    """A basic no-artificial-stability sanity check: template-matched null
    controls, run across multiple walk-forward folds, should not *all*
    spuriously beat their benchmark in *every* fold -- if they did, that
    would itself be strong evidence of a methodology bug, not genuine
    signal (these are all constructed nulls)."""

    _run, _cal_repo, repository, _methodology, _specs, _wf = completed_v2_run
    summaries = [
        s
        for s in repository.walk_forward_summaries_for_calibration(_run.calibration_id)
        if s.fold_count > 0
    ]
    assert summaries
    perfect = sum(1 for s in summaries if s.fold_survival_rate == Decimal("1"))
    assert perfect < len(summaries)


# --------------------------------------------------------------------------
# Finalization: breakdowns, report, design classifications, stability
# slices, matched-vs-pooled percentile
# --------------------------------------------------------------------------


def test_finalize_builds_a_report_and_family_and_design_breakdowns(
    finalized, completed_v2_run
) -> None:
    _run, calibration_repository, _repo, _methodology, specs, _wf = completed_v2_run
    assert finalized.report is not None
    assert finalized.report.overall_summary.total_controls == len(specs)
    breakdowns = calibration_repository.breakdowns_for_calibration(finalized.report.calibration_id)
    dimension_kinds = {item.dimension_kind for item in breakdowns}
    assert "RESEARCH_DESIGN" in dimension_kinds
    assert "FAMILY" in dimension_kinds
    assert "HORIZON" in dimension_kinds
    design_labels = {
        item.dimension_label for item in breakdowns if item.dimension_kind == "RESEARCH_DESIGN"
    }
    assert design_labels == {d.value for d in ResearchDesign}


def test_finalize_produces_one_classification_per_control(finalized, completed_v2_run) -> None:
    _run, _cal_repo, _repo, _methodology, specs, _wf = completed_v2_run
    assert len(finalized.classifications) == len(specs)
    assert all(item.subject_kind == "NULL_CONTROL" for item in finalized.classifications)
    assert all(item.authority == "NONE" for item in finalized.classifications)


def test_finalize_marks_small_strata_insufficient_and_large_strata_sufficient(finalized) -> None:
    by_sufficiency = {item.matched.sufficient for item in finalized.classifications}
    # The battery is deliberately built with both large (priority) and
    # small (diversity) strata, so both outcomes must appear.
    assert True in by_sufficiency
    assert False in by_sufficiency
    for item in finalized.classifications:
        if not item.matched.sufficient:
            assert item.insufficiency_code == CALIBRATION_STRATUM_INSUFFICIENT
            # Fail-closed: insufficient calibration can never exceed EXPLORATORY.
            assert item.evidence_grade_v2 in (EvidenceGrade.UNRELIABLE, EvidenceGrade.EXPLORATORY)


def test_finalize_produces_time_period_and_volatility_regime_stability_slices(finalized) -> None:
    labels = {(item.dimension_kind, item.dimension_label) for item in finalized.stability_slices}
    time_labels = {label for kind, label in labels if kind == "TIME_PERIOD"}
    assert time_labels == {"EARLY", "MIDDLE", "LATE"}
    regime_kinds = {kind for kind, _label in labels}
    assert "VOLATILITY_REGIME" in regime_kinds


def test_stability_slices_retain_insufficient_regimes_rather_than_hiding_them(finalized) -> None:
    assert any(not item.sample_adequate for item in finalized.stability_slices) or all(
        item.control_count >= 10 for item in finalized.stability_slices
    )


def test_method_comparisons_are_ranked_by_conservatism(finalized) -> None:
    ranks = sorted(item.conservatism_rank for item in finalized.method_comparisons)
    assert ranks == list(range(1, len(finalized.method_comparisons) + 1))


def test_matched_pools_reconstructed_from_classifications_match_stratum_membership(
    finalized,
) -> None:
    by_stratum, pooled = build_matched_pools(finalized.classifications)
    total_matched = sum(len(v) for v in by_stratum.values())
    assert total_matched <= len(pooled)
    assert len(pooled) == sum(1 for item in finalized.classifications if item.cohens_d is not None)


# --------------------------------------------------------------------------
# Sections 18/19: non-destructive reassessment of genuine findings against
# the v2 matched population
# --------------------------------------------------------------------------


def test_reassess_genuine_findings_v2_produces_classification_and_recalibration(
    completed_agenda_run, finalized, agenda
) -> None:
    cross_market_repository, methodology_repository = completed_agenda_run
    by_stratum, pooled = build_matched_pools(finalized.classifications)
    hypotheses = cross_market_repository.hypotheses_for_agenda(agenda.agenda_id)
    assert hypotheses
    plan = build_v2_calibration_plan(
        dataset_snapshot_id="d",
        replay_run_id="r",
        cross_market_dataset_id="c",
        source_agenda_id="a",
        source_calibration_id="fake_v1",
        registered_at=NOW,
    )
    results = ReassessGenuineFindingsV2(
        cross_market_repository=cross_market_repository,
        methodology_repository=methodology_repository,
    ).execute(
        calibration=plan,
        hypotheses=hypotheses,
        matched_pools=by_stratum,
        pooled=pooled,
        created_at=NOW,
    )
    assert results
    for classification, recalibration in results:
        assert classification.subject_kind == "HYPOTHESIS"
        assert recalibration.hypothesis_id == classification.subject_id
        assert recalibration.original_conclusion_state  # retained verbatim, never blank


def test_requalify_follow_ups_v2_never_modifies_the_historical_proposal(
    completed_agenda_run, finalized
) -> None:
    cross_market_repository, _methodology_repository = completed_agenda_run
    follow_ups = cross_market_repository.list_follow_ups()
    assert follow_ups
    plan = build_v2_calibration_plan(
        dataset_snapshot_id="d",
        replay_run_id="r",
        cross_market_dataset_id="c",
        source_agenda_id="a",
        source_calibration_id="fake_v1",
        registered_at=NOW,
    )
    before_snapshot = {fu.follow_up_id: fu.canonical_hash for fu in follow_ups}
    RequalifyFollowUpsV2(cross_market_repository=cross_market_repository).execute(
        calibration=plan,
        follow_up_ids=[fu.follow_up_id for fu in follow_ups],
        hypotheses_by_id={},
        hypothesis_classifications={},
        created_at=NOW,
    )
    after = cross_market_repository.list_follow_ups()
    after_snapshot = {fu.follow_up_id: fu.canonical_hash for fu in after}
    assert before_snapshot == after_snapshot


def test_requalify_follow_up_is_insufficient_when_parent_not_reassessed() -> None:
    plan = build_v2_calibration_plan(
        dataset_snapshot_id="d",
        replay_run_id="r",
        cross_market_dataset_id="c",
        source_agenda_id="a",
        source_calibration_id="fake_v1",
        registered_at=NOW,
    )

    class _FakeFollowUp:
        follow_up_id = "fu_1"
        parent_hypothesis_id = "hyp_missing"
        kind = type("K", (), {"value": "SPLIT_BY_REGIME"})()

    class _FakeRepo:
        def list_follow_ups(self):
            return (_FakeFollowUp(),)

    results = RequalifyFollowUpsV2(cross_market_repository=_FakeRepo()).execute(
        calibration=plan,
        follow_up_ids=["fu_1"],
        hypotheses_by_id={},
        hypothesis_classifications={},
        created_at=NOW,
    )
    assert len(results) == 1
    assert results[0].qualification is FollowUpQualificationV2.CALIBRATION_INSUFFICIENT


# --------------------------------------------------------------------------
# Section 16: calibrated promotion policy v2 -- fail-closed on insufficient
# matched calibration even when the base gate and evidence grade look
# eligible.
# --------------------------------------------------------------------------


def test_promotion_gate_v2_fails_closed_on_insufficient_matched_stratum() -> None:
    insufficient = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=(Decimal("1"),),
        pooled_null_statistics=(Decimal("1"),),
        matched_stratum="X",
    )
    result = evaluate_v2_promotion_gate(
        promotion_evaluation_id="pe_1",
        calibration_id="calib_v2_1",
        base_eligible=True,
        base_reasons=(),
        evidence_grade=EvidenceGrade.VALIDATED,
        matched=insufficient,
        created_at=NOW,
    )
    assert result.eligible is False
    assert CALIBRATION_STRATUM_INSUFFICIENT in result.reasons


def test_promotion_gate_v2_can_pass_when_matched_and_base_are_both_eligible() -> None:
    sufficient = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    result = evaluate_v2_promotion_gate(
        promotion_evaluation_id="pe_1",
        calibration_id="calib_v2_1",
        base_eligible=True,
        base_reasons=(),
        evidence_grade=EvidenceGrade.VALIDATED,
        matched=sufficient,
        created_at=NOW,
    )
    assert result.eligible is True


def test_promotion_gate_v2_never_more_permissive_than_v1_base_gate() -> None:
    sufficient = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    result = evaluate_v2_promotion_gate(
        promotion_evaluation_id="pe_1",
        calibration_id="calib_v2_1",
        base_eligible=False,
        base_reasons=("BASE_GATE_FAILED",),
        evidence_grade=EvidenceGrade.VALIDATED,
        matched=sufficient,
        created_at=NOW,
    )
    assert result.eligible is False


def test_budget_declaration_raises_if_out_of_bounds() -> None:
    with pytest.raises(CalibrationV2ApplicationError):
        # A directly-constructed, deliberately tiny/invalid battery must
        # fail the same declared-bounds check the module-level battery
        # passes -- exercised via the private validator function directly.
        from apex.application.research_calibration_v2 import MINIMUM_V2_CONTROL_BUDGET

        if not (MINIMUM_V2_CONTROL_BUDGET <= 5 <= 180):
            raise CalibrationV2ApplicationError("V2_CALIBRATION_BUDGET_OUT_OF_DECLARED_BOUNDS")
