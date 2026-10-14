"""Domain-level proofs for controlled cross-market hypothesis research:
the agenda budget, feature/regime math, hypothesis templates, negative-
control construction, and the knowledge/contradiction/follow-up/quality
records -- no adapters, no database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.cross_market_research import (
    REGIME_CONDITION_FEATURE_IDS,
    AgendaQualityReport,
    AgendaRun,
    AgendaRunStatus,
    ConditionRule,
    CrossMarketRegimeDimension,
    CrossMarketResearchError,
    DatedValue,
    DiscoveryStatus,
    FeatureFamily,
    FeatureId,
    FollowUpKind,
    FollowUpProposal,
    GeneratedHypothesis,
    HypothesisExecutionRecord,
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
    StabilityState,
    SubperiodResult,
    basket_return,
    build_cross_market_regime_snapshot,
    cross_sectional_dispersion,
    derive_stability_state,
    deterministic_noise_series,
    deterministic_shuffle,
    fraction_positive,
    lag_displace,
    rolling_correlation_series,
    rolling_drawdown_series,
    rolling_returns,
    rolling_volatility_series,
    series_correlation,
    series_return,
)

NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _agenda(**overrides: object) -> ResearchAgenda:
    defaults: dict[str, object] = dict(
        version="V1",
        objective="Bounded controlled cross-market hypothesis research.",
        allowed_markets=tuple(MarketId),
        allowed_feature_families=tuple(FeatureFamily),
        allowed_outcome_families=tuple(OutcomeFamily),
        allowed_horizons_sessions=(5, 20, 60),
        allowed_templates=tuple(HypothesisTemplateId),
        hypothesis_generation_method=HypothesisGenerationMethod.DETERMINISTIC_TEMPLATE_EXPANSION_V1,
        maximum_hypothesis_count=25,
        maximum_configurations_per_hypothesis=1,
        maximum_total_trials=400,
        train_validation_holdout_policy="TRAIN<VALIDATION<HOLDOUT(locked)",
        multiple_testing_family_policy="PARTITIONED_BY_HYPOTHESIS_FAMILY_V1",
        robustness_requirements=("SUBPERIOD_SPLIT",),
        dataset_snapshot_id="historical_dataset_snapshot_x",
        replay_run_id="historical_replay_run_x",
        cross_market_dataset_id="cross_market_dataset_x",
        cohort_policy_id="research_cohort_policy_x",
        registered_at=NOW,
    )
    defaults.update(overrides)
    return ResearchAgenda.create(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Agenda: bounded hypothesis count, bounded configurations, immutable budget
# --------------------------------------------------------------------------


def test_agenda_rejects_unbounded_hypothesis_count() -> None:
    with pytest.raises(CrossMarketResearchError, match="AGENDA_HYPOTHESIS_COUNT"):
        _agenda(maximum_hypothesis_count=31)
    with pytest.raises(CrossMarketResearchError, match="AGENDA_HYPOTHESIS_COUNT"):
        _agenda(maximum_hypothesis_count=0)


def test_agenda_rejects_unbounded_configurations_per_hypothesis() -> None:
    with pytest.raises(CrossMarketResearchError, match="AGENDA_CONFIGURATIONS"):
        _agenda(maximum_configurations_per_hypothesis=5)


def test_agenda_rejects_total_trial_budget_smaller_than_its_own_bounds() -> None:
    with pytest.raises(CrossMarketResearchError, match="AGENDA_TOTAL_TRIAL_BUDGET_TOO_SMALL"):
        _agenda(
            maximum_hypothesis_count=25,
            maximum_configurations_per_hypothesis=1,
            maximum_total_trials=10,
        )


def test_agenda_rejects_unbounded_total_trial_budget() -> None:
    with pytest.raises(CrossMarketResearchError, match="AGENDA_TOTAL_TRIAL_BUDGET_UNBOUNDED"):
        _agenda(maximum_total_trials=2001)


def test_agenda_identity_is_a_pure_function_of_its_declared_fields() -> None:
    """The search-space budget is immutable once registered: declaring it
    twice with identical fields always produces the identical agenda_id,
    and any single differing field produces a genuinely different one."""

    first = _agenda()
    second = _agenda()
    assert first.agenda_id == second.agenda_id
    third = _agenda(maximum_hypothesis_count=24)
    assert third.agenda_id != first.agenda_id


def test_agenda_requires_at_least_one_market_family_and_template() -> None:
    with pytest.raises(CrossMarketResearchError):
        _agenda(allowed_markets=())
    with pytest.raises(CrossMarketResearchError):
        _agenda(allowed_feature_families=())
    with pytest.raises(CrossMarketResearchError):
        _agenda(allowed_templates=())


def test_agenda_horizon_set_must_stay_small() -> None:
    with pytest.raises(CrossMarketResearchError, match="AGENDA_HORIZON_SET"):
        _agenda(allowed_horizons_sessions=(1, 2, 3, 4, 5, 6))


# --------------------------------------------------------------------------
# Agenda run: the one bounded mutable cursor -- "conflicting replay"
# --------------------------------------------------------------------------


def test_agenda_run_advance_is_forward_only_and_fails_closed_from_terminal_state() -> None:
    agenda = _agenda()
    run = AgendaRun.draft(agenda=agenda, hypothesis_ids=("h1", "h2"), created_at=NOW)
    assert run.status is AgendaRunStatus.NOT_STARTED
    run = run.advance(hypothesis_id="h1", trials_added=3, is_final=False, at=NOW)
    assert run.status is AgendaRunStatus.IN_PROGRESS
    run = run.advance(hypothesis_id="h2", trials_added=4, is_final=True, at=NOW)
    assert run.status is AgendaRunStatus.COMPLETE
    assert run.trials_executed == 7
    with pytest.raises(CrossMarketResearchError, match="TERMINAL_STATE"):
        run.advance(hypothesis_id="h3", trials_added=1, is_final=False, at=NOW)


def test_agenda_run_replay_with_conflicting_hypothesis_set_is_a_genuinely_different_run() -> None:
    """Redrafting an agenda run for the same agenda but a genuinely
    different hypothesis set (a "conflicting replay") must not silently
    collide with the original run's identity."""

    agenda = _agenda()
    run_a = AgendaRun.draft(agenda=agenda, hypothesis_ids=("h1", "h2"), created_at=NOW)
    run_b = AgendaRun.draft(agenda=agenda, hypothesis_ids=("h1", "h2", "h3"), created_at=NOW)
    assert run_a.agenda_run_id != run_b.agenda_run_id
    assert run_a.canonical_input_hash != run_b.canonical_input_hash


def test_agenda_run_requires_hypothesis_ids_within_agenda_budget() -> None:
    agenda = _agenda(maximum_hypothesis_count=2)
    with pytest.raises(CrossMarketResearchError, match="EXCEEDS_AGENDA_BUDGET"):
        AgendaRun.draft(agenda=agenda, hypothesis_ids=("h1", "h2", "h3"), created_at=NOW)


# --------------------------------------------------------------------------
# Features/regimes: point-in-time gating, missing input, regime stability
# --------------------------------------------------------------------------


def test_series_return_is_none_when_lookback_is_incomplete_not_silently_shortened() -> None:
    series = tuple(DatedValue(f"2024-01-{d:02d}", Decimal(100 + d)) for d in range(1, 10))
    assert series_return(series, as_of_date="2024-01-05", window_sessions=20) is None


def test_series_return_never_uses_a_value_dated_after_as_of() -> None:
    """Point-in-time gating: appending future dates to the series must not
    change a return computed as of an earlier date."""

    base = tuple(DatedValue(f"2024-01-{d:02d}", Decimal(100 + d)) for d in range(1, 25))
    future = (*base, DatedValue("2024-02-01", Decimal(9999)))
    assert series_return(base, as_of_date="2024-01-24", window_sessions=20) == series_return(
        future, as_of_date="2024-01-24", window_sessions=20
    )


def test_missing_correlation_input_returns_none_not_a_fabricated_value() -> None:
    short_a = tuple(DatedValue(f"2024-01-{d:02d}", Decimal(100 + d)) for d in range(1, 5))
    short_b = tuple(DatedValue(f"2024-01-{d:02d}", Decimal(200 - d)) for d in range(1, 5))
    assert series_correlation(short_a, short_b, as_of_date="2024-01-04", window_sessions=60) is None


def test_cross_sectional_dispersion_and_fraction_positive_handle_empty_and_thin_input() -> None:
    assert cross_sectional_dispersion(()) is None
    assert cross_sectional_dispersion((Decimal("0.01"),)) is None
    assert fraction_positive(()) is None
    assert fraction_positive((Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"))) == Decimal(
        "0.66666667"
    )


def test_basket_return_excludes_members_with_incomplete_lookback_rather_than_zero_filling() -> None:
    long_series = tuple(DatedValue(f"2024-01-{d:02d}", Decimal(100 + d)) for d in range(1, 25))
    short_series = tuple(DatedValue(f"2024-01-{d:02d}", Decimal(100 + d)) for d in range(1, 5))
    only_long = basket_return([("A", long_series)], as_of_date="2024-01-24", window_sessions=20)
    mixed = basket_return(
        [("A", long_series), ("B", short_series)], as_of_date="2024-01-24", window_sessions=20
    )
    assert only_long == mixed  # B is silently excluded, not treated as a zero return


def test_rolling_batch_functions_match_their_single_as_of_counterparts() -> None:
    values = [Decimal(100 + i) for i in range(60)]
    dates = [f"d{i:03d}" for i in range(60)]
    series = tuple(DatedValue(d, v) for d, v in zip(dates, values, strict=True))
    batch = rolling_returns(values, window_sessions=20)
    assert batch[40] == series_return(series, as_of_date=dates[40], window_sessions=20)
    batch_vol = rolling_volatility_series(values, window_sessions=20, sessions_per_year=252)
    assert batch_vol[40] is not None
    batch_dd = rolling_drawdown_series(values)
    assert batch_dd[-1] == Decimal("0E-8")  # monotonically increasing -> zero drawdown


def test_rolling_correlation_series_requires_equal_length_aligned_arrays() -> None:
    with pytest.raises(CrossMarketResearchError, match="LENGTH_MISMATCH"):
        rolling_correlation_series([Decimal(1), Decimal(2)], [Decimal(1)], window_sessions=2)


def test_regime_snapshot_has_ten_independently_interpretable_dimensions() -> None:
    snapshot = build_cross_market_regime_snapshot(
        equity_return_20=Decimal("0.08"),
        equity_fraction_advancing=Decimal("0.65"),
        equity_volatility_20=Decimal("0.12"),
        equity_dispersion_20=Decimal("0.01"),
        equity_average_correlation_60=Decimal("0.5"),
        btc_return_20=Decimal("0.2"),
        btc_volatility_20=Decimal("0.6"),
        rates_proxy_return_20=Decimal("-0.03"),
        rates_equity_correlation_60=Decimal("-0.4"),
        commodity_proxy_return_20=Decimal("0.02"),
    )
    assert len(snapshot.classifications) == 10
    assert snapshot.label_for(CrossMarketRegimeDimension.BOND_RISK_RELATIONSHIP) == "RATE_HEADWIND"
    assert snapshot.label_for(CrossMarketRegimeDimension.EQUITY_RISK_TONE) in (
        "RISK_ON",
        "NEUTRAL",
        "RISK_OFF",
        "INSUFFICIENT_EVIDENCE",
    )


def test_regime_snapshot_reports_insufficient_evidence_rather_than_fabricating_a_label() -> None:
    snapshot = build_cross_market_regime_snapshot(
        equity_return_20=None,
        equity_fraction_advancing=None,
        equity_volatility_20=None,
        equity_dispersion_20=None,
        equity_average_correlation_60=None,
        btc_return_20=None,
        btc_volatility_20=None,
        rates_proxy_return_20=None,
        rates_equity_correlation_60=None,
        commodity_proxy_return_20=None,
    )
    assert snapshot.label_for(CrossMarketRegimeDimension.BOND_RISK_RELATIONSHIP) == (
        "INSUFFICIENT_EVIDENCE"
    )
    assert snapshot.label_for(CrossMarketRegimeDimension.EQUITY_TREND) == "INSUFFICIENT_EVIDENCE"


def test_every_regime_condition_feature_id_resolves_to_a_real_dimension() -> None:
    for feature_id, dimension in REGIME_CONDITION_FEATURE_IDS.items():
        assert isinstance(feature_id, FeatureId)
        assert isinstance(dimension, CrossMarketRegimeDimension)


def test_derive_stability_state_distinguishes_stable_unstable_and_regime_dependent() -> None:
    stable = [
        SubperiodResult("A", "2020-01-01", "2020-06-30", 40, Decimal("0.02"), False),
        SubperiodResult("B", "2020-07-01", "2020-12-31", 40, Decimal("0.03"), False),
        SubperiodResult("C", "2021-01-01", "2021-06-30", 40, Decimal("0.01"), False),
    ]
    assert derive_stability_state(stable) is StabilityState.STABLE

    regime_dependent = [
        SubperiodResult("A", "2020-01-01", "2020-06-30", 40, Decimal("0.02"), False),
        SubperiodResult("B", "2020-07-01", "2020-12-31", 40, Decimal("-0.05"), True),
        SubperiodResult("C", "2021-01-01", "2021-06-30", 40, Decimal("0.01"), False),
    ]
    assert derive_stability_state(regime_dependent) is StabilityState.REGIME_DEPENDENT

    thin = [SubperiodResult("A", "2020-01-01", "2020-06-30", 2, Decimal("0.02"), False)]
    assert derive_stability_state(thin) is StabilityState.INSUFFICIENT_EVIDENCE


# --------------------------------------------------------------------------
# Deterministic proposal generation: templates, negative controls
# --------------------------------------------------------------------------


def _hypothesis(agenda: ResearchAgenda, **overrides: object) -> GeneratedHypothesis:
    defaults: dict[str, object] = dict(
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
        rationale="Breadth may carry information about subsequent broad-market return.",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=False,
        negative_control_kind=None,
        discovery_status=DiscoveryStatus.PREDECLARED,
        ordinal=1,
        created_at=NOW,
    )
    defaults.update(overrides)
    return GeneratedHypothesis.create(**defaults)  # type: ignore[arg-type]


def test_hypothesis_template_must_be_approved_by_its_agenda() -> None:
    agenda = _agenda(allowed_templates=(HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,))
    with pytest.raises(CrossMarketResearchError, match="TEMPLATE_NOT_APPROVED"):
        _hypothesis(agenda, template_id=HypothesisTemplateId.INTERACTION_V1)


def test_conditional_and_interaction_templates_require_a_secondary_feature() -> None:
    agenda = _agenda()
    with pytest.raises(CrossMarketResearchError, match="CONDITIONAL_TEMPLATE_REQUIRES"):
        _hypothesis(agenda, template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1)
    with pytest.raises(CrossMarketResearchError, match="INTERACTION_TEMPLATE_REQUIRES"):
        _hypothesis(agenda, template_id=HypothesisTemplateId.INTERACTION_V1)


def test_hypothesis_horizon_must_be_approved_by_its_agenda() -> None:
    agenda = _agenda(allowed_horizons_sessions=(5, 20))
    with pytest.raises(CrossMarketResearchError, match="HORIZON_NOT_APPROVED"):
        _hypothesis(agenda, horizon_sessions=60)


def test_assisted_model_proposals_are_rejected_at_the_hypothesis_layer_when_used_directly() -> None:
    """Section 5's disabled-by-default assisted seam: even a caller that
    tries to construct a hypothesis directly with the assisted source
    (bypassing the seam's own disabled-by-default guard) is rejected."""

    agenda = _agenda()
    with pytest.raises(CrossMarketResearchError, match="ASSISTED_MODEL_PROPOSALS_ARE_DISABLED"):
        _hypothesis(agenda, proposal_source=ProposalSource.ASSISTED_MODEL_SEAM)


def test_negative_control_flag_must_match_discovery_status() -> None:
    agenda = _agenda()
    with pytest.raises(CrossMarketResearchError, match="NEGATIVE_CONTROL_FLAG_MUST_MATCH"):
        _hypothesis(agenda, is_negative_control=True, discovery_status=DiscoveryStatus.PREDECLARED)


def test_negative_control_requires_a_recognized_kind() -> None:
    agenda = _agenda()
    with pytest.raises(CrossMarketResearchError, match="NEGATIVE_CONTROL_KIND_INVALID"):
        _hypothesis(
            agenda,
            is_negative_control=True,
            discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
            negative_control_kind="NOT_A_REAL_KIND",
        )


def test_two_hypotheses_with_identical_fields_are_the_same_duplicate_identity() -> None:
    """Section 4/6: identical declared fields must be recognized as the
    same hypothesis (a duplicate), not two independent trials consuming
    twice the multiple-testing budget."""

    agenda = _agenda()
    first = _hypothesis(agenda, ordinal=1)
    second = _hypothesis(agenda, ordinal=1)
    assert first.hypothesis_id == second.hypothesis_id
    different = _hypothesis(agenda, ordinal=2, horizon_sessions=5)
    assert different.hypothesis_id != first.hypothesis_id


def test_deterministic_shuffle_is_seed_reproducible_and_destroys_alignment() -> None:
    series = tuple(DatedValue(f"d{i:03d}", Decimal(i)) for i in range(50))
    shuffled_once = deterministic_shuffle(series, seed=42)
    shuffled_twice = deterministic_shuffle(series, seed=42)
    assert shuffled_once == shuffled_twice
    assert shuffled_once != series
    assert sorted(v.value for v in shuffled_once) == sorted(v.value for v in series)


def test_lag_displace_shifts_values_onto_later_dates_deterministically() -> None:
    series = tuple(DatedValue(f"d{i:03d}", Decimal(i)) for i in range(10))
    lagged = lag_displace(series, sessions=3)
    assert len(lagged) == 7
    assert lagged[0] == DatedValue("d003", Decimal(0))


def test_deterministic_noise_series_is_seed_reproducible_and_unrelated_to_input_dates() -> None:
    dates = [f"d{i:03d}" for i in range(20)]
    first = deterministic_noise_series(dates, seed=7)
    second = deterministic_noise_series(dates, seed=7)
    assert first == second
    assert all(Decimal(0) <= item.value < Decimal(1) for item in first)


# --------------------------------------------------------------------------
# Knowledge base: supported/weak/rejected, conclusion history, contradiction,
# follow-up lineage
# --------------------------------------------------------------------------


def _knowledge_entry(**overrides: object) -> ResearchKnowledgeEntry:
    defaults: dict[str, object] = dict(
        hypothesis_id="cross_market_hypothesis_x",
        conclusion_id="research_conclusion_x",
        status=KnowledgeStatus.WEAK,
        evidence_strength="WEAK_SUPPORT",
        phase_reached="LOCKED_HOLDOUT",
        markets=(MarketId.US_EQUITY, MarketId.BTC),
        regime_dependence="STABLE",
        benchmark_kind="UNCONDITIONAL_SUBSEQUENT_RETURN",
        holdout_result="DOES_NOT_BEAT_BENCHMARK",
        robustness_state="PASSED",
        cross_sectional_or_time_series="TIME_SERIES_PANEL_WITH_SHARED_DATES",
        limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        revision=1,
        supersedes_entry_id=None,
        created_at=NOW,
    )
    defaults.update(overrides)
    return ResearchKnowledgeEntry.create(**defaults)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "status",
    [KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY, KnowledgeStatus.WEAK, KnowledgeStatus.REJECTED],
)
def test_knowledge_entry_accepts_every_retained_status(status: KnowledgeStatus) -> None:
    entry = _knowledge_entry(status=status)
    assert entry.status is status


def test_knowledge_entry_revision_lineage_must_be_declared_consistently() -> None:
    with pytest.raises(CrossMarketResearchError, match="REVISION_LINEAGE_INVALID"):
        _knowledge_entry(revision=2, supersedes_entry_id=None)
    with pytest.raises(CrossMarketResearchError, match="REVISION_LINEAGE_INVALID"):
        _knowledge_entry(revision=1, supersedes_entry_id="cross_market_knowledge_entry_x")


def test_knowledge_entry_history_is_never_overwritten_only_superseded() -> None:
    revision_one = _knowledge_entry(
        status=KnowledgeStatus.WEAK, revision=1, supersedes_entry_id=None
    )
    revision_two = _knowledge_entry(
        status=KnowledgeStatus.REJECTED,
        conclusion_id="research_conclusion_y",
        revision=2,
        supersedes_entry_id=revision_one.entry_id,
    )
    assert revision_two.supersedes_entry_id == revision_one.entry_id
    assert revision_one.entry_id != revision_two.entry_id
    assert revision_one.status is KnowledgeStatus.WEAK  # unchanged by the later revision


def test_contradiction_requires_two_distinct_entries_and_is_order_independent() -> None:
    with pytest.raises(CrossMarketResearchError, match="TWO_DISTINCT_ENTRIES"):
        ResearchContradiction.create(
            first_entry_id="e1",
            second_entry_id="e1",
            shared_evidence=(),
            first_holds_when="x",
            second_holds_when="y",
            description="d",
            created_at=NOW,
        )
    forward = ResearchContradiction.create(
        first_entry_id="e1",
        second_entry_id="e2",
        shared_evidence=("btc",),
        first_holds_when="RISK_ON",
        second_holds_when="RISK_OFF",
        description="Conflicting sign of effect across regimes.",
        created_at=NOW,
    )
    backward = ResearchContradiction.create(
        first_entry_id="e2",
        second_entry_id="e1",
        shared_evidence=("btc",),
        first_holds_when="RISK_OFF",
        second_holds_when="RISK_ON",
        description="Conflicting sign of effect across regimes.",
        created_at=NOW,
    )
    assert forward.first_entry_id == "e1"
    assert forward.contradiction_id == backward.contradiction_id
    assert forward.resolved is False


def test_follow_up_proposal_retains_parent_lineage_and_never_grants_authority() -> None:
    follow_up = FollowUpProposal.create(
        parent_conclusion_id="research_conclusion_x",
        parent_hypothesis_id="cross_market_hypothesis_x",
        kind=FollowUpKind.SPLIT_BY_REGIME,
        proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        description="Split by BTC volatility regime given observed instability.",
        created_at=NOW,
    )
    assert follow_up.parent_conclusion_id == "research_conclusion_x"
    assert follow_up.requires_new_agenda is True
    assert follow_up.authority == "NONE"


def test_follow_up_requires_parent_lineage() -> None:
    with pytest.raises(CrossMarketResearchError, match="PARENT_LINEAGE_REQUIRED"):
        FollowUpProposal.create(
            parent_conclusion_id="",
            parent_hypothesis_id="cross_market_hypothesis_x",
            kind=FollowUpKind.SPLIT_BY_REGIME,
            proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
            description="d",
            created_at=NOW,
        )


def test_hypothesis_execution_record_authority_is_always_none() -> None:
    record = HypothesisExecutionRecord.create(
        hypothesis_id="cross_market_hypothesis_x",
        agenda_id="cross_market_research_agenda_x",
        governance_policy_id="research_governance_policy_x",
        exploratory_trial_id=None,
        validation_trial_id=None,
        holdout_trial_id=None,
        walk_forward_trial_ids=(),
        robustness_run_id=None,
        trial_family_id=None,
        conclusion_id=None,
        promotion_evaluation_id=None,
        stability=None,
        evidence_shape="TIME_SERIES_PANEL_WITH_SHARED_DATES",
        blockers=("VALIDATION_SAMPLE_INSUFFICIENT",),
        created_at=NOW,
    )
    assert record.authority == "NONE"


# --------------------------------------------------------------------------
# Statistical honesty / research-quality report
# --------------------------------------------------------------------------


def test_quality_report_retains_negative_control_and_panel_dependence_language() -> None:
    report = AgendaQualityReport.create(
        agenda_id="cross_market_research_agenda_x",
        dataset_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE", "SURVIVORSHIP_LIMITED"),
        provider_coverage_limitations=("HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",),
        panel_dependence_note=(
            "Security-date rows share market dates and are not independent samples."
        ),
        multiple_testing_burden_note="25 hypotheses across 5 families; BH-FDR applied per family.",
        negative_control_behavior_note="6 negative controls retained.",
        holdout_integrity_note="Holdout locked before any holdout trial was recorded.",
        conclusion_distribution=(("WEAK", 2), ("REJECTED", 15), ("ACTIVE_RESEARCH", 8)),
        hypotheses_proposed=25,
        trials_run=150,
        created_at=NOW,
    )
    assert "not independent samples" in report.panel_dependence_note
    assert report.authority == "NONE"
    assert sum(count for _label, count in report.conclusion_distribution) == 25


def test_causal_language_policy_explicitly_prohibits_causal_wording() -> None:
    from apex.domain.cross_market_research import (
        CAUSAL_LANGUAGE_POLICY,
        cross_market_research_warnings,
    )

    lowered = CAUSAL_LANGUAGE_POLICY.lower()
    assert "may not conclude" in lowered and "causes" in lowered
    assert "historically associated with" in lowered
    assert "conditionally predictive" in lowered
    warnings = cross_market_research_warnings()
    assert any("NOT_INDEPENDENT_SAMPLES" in warning for warning in warnings)
    assert any("DISCOVERED" in warning for warning in warnings)
