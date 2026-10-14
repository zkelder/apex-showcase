"""Domain-level proofs for Cross-Market Research v3: the agenda budget,
corporate feature/outcome vocabulary, hypothesis construction, panel-
dependence diagnostics, incremental-contribution/contribution-accounting
records, and the forward options seam -- no adapters, no database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.corporate_market_research import (
    ALLOWED_HORIZONS_SESSIONS,
    CORPORATE_NEGATIVE_CONTROL_KINDS,
    GENUINE_CORPORATE_FEATURE_IDS,
    CorporateContributionEvidence,
    CorporateFeatureId,
    CorporateHypothesisFamily,
    CorporateMarketResearchAgenda,
    CorporateMarketResearchError,
    CorporateOutcomeId,
    CorporatePanelRow,
    GeneratedCorporateHypothesis,
    IncrementalContributionResult,
    OptionsForwardSeamSnapshot,
    compute_panel_dependence_diagnostics,
    pearson_correlation,
    sector_relative_return,
)
from apex.domain.cross_market_research import ConditionRule, DiscoveryStatus, ProposalSource
from apex.domain.research_methodology import BenchmarkKind, OverlapHandlingMode

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _agenda(**overrides: object) -> CorporateMarketResearchAgenda:
    defaults: dict[str, object] = dict(
        version="V3",
        objective="Determine whether corporate evidence adds value beyond market context.",
        corporate_dataset_id="corporate-dataset-1",
        equity_snapshot_id="equity-snapshot-1",
        replay_run_id="replay-run-1",
        cross_market_dataset_id="cross-market-dataset-1",
        cohort_policy_id="cohort-policy-1",
        allowed_feature_ids=tuple(CorporateFeatureId),
        allowed_outcome_ids=tuple(CorporateOutcomeId),
        allowed_horizons_sessions=ALLOWED_HORIZONS_SESSIONS,
        allowed_templates=(
            "CROSS_SECTIONAL_V1",
            "CORPORATE_CHANGE_V1",
            "FILING_EVENT_V1",
            "CONDITIONAL_RELATIONSHIP_V1",
            "INTERACTION_V1",
        ),
        maximum_genuine_hypothesis_count=20,
        maximum_hypothesis_count=37,
        maximum_configurations_per_hypothesis=3,
        maximum_total_trials=150,
        train_validation_holdout_policy="70/15/15 split",
        multiple_testing_family_policy="BH-FDR per family",
        robustness_requirements=("LEAVE_ONE_PERIOD_OUT",),
        lookback_years=6,
        registered_at=NOW,
    )
    defaults.update(overrides)
    return CorporateMarketResearchAgenda.create(**defaults)  # type: ignore[arg-type]


# ==========================================================================
# Agenda budget (section 1)
# ==========================================================================


def test_agenda_requires_genuine_hypothesis_count_between_12_and_24() -> None:
    with pytest.raises(CorporateMarketResearchError):
        _agenda(maximum_genuine_hypothesis_count=11)
    with pytest.raises(CorporateMarketResearchError):
        _agenda(maximum_genuine_hypothesis_count=25)
    assert _agenda(maximum_genuine_hypothesis_count=20).maximum_genuine_hypothesis_count == 20


def test_agenda_requires_every_genuine_corporate_feature_allowed() -> None:
    incomplete = tuple(GENUINE_CORPORATE_FEATURE_IDS)[:-1]
    with pytest.raises(CorporateMarketResearchError):
        _agenda(allowed_feature_ids=incomplete)


def test_agenda_rejects_unbounded_horizons() -> None:
    with pytest.raises(CorporateMarketResearchError):
        _agenda(allowed_horizons_sessions=(5, 20, 61))


def test_agenda_requires_every_negative_control_kind_declared() -> None:
    with pytest.raises(CorporateMarketResearchError):
        _agenda(negative_control_kinds=CORPORATE_NEGATIVE_CONTROL_KINDS[:-1])


def test_agenda_trial_budget_cannot_be_smaller_than_hypothesis_count() -> None:
    with pytest.raises(CorporateMarketResearchError):
        _agenda(maximum_total_trials=10)


def test_agenda_trial_budget_is_bounded() -> None:
    with pytest.raises(CorporateMarketResearchError):
        _agenda(maximum_total_trials=1_001)


def test_agenda_is_content_addressed_and_deterministic() -> None:
    first = _agenda()
    second = _agenda()
    assert first.agenda_id == second.agenda_id
    assert first.canonical_hash == second.canonical_hash
    assert first.authority == "NONE"


# ==========================================================================
# Hypothesis construction (sections 2, 5, 9): every genuine hypothesis
# structurally requires a real corporate feature id.
# ==========================================================================


def _hypothesis(
    agenda: CorporateMarketResearchAgenda, **overrides: object
) -> GeneratedCorporateHypothesis:
    defaults: dict[str, object] = dict(
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
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
        rationale="Test rationale.",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=False,
        negative_control_kind=None,
        discovery_status=DiscoveryStatus.PREDECLARED,
        ordinal=0,
        created_at=NOW,
    )
    defaults.update(overrides)
    return GeneratedCorporateHypothesis.create(**defaults)  # type: ignore[arg-type]


def test_genuine_hypothesis_feature_id_is_never_optional() -> None:
    agenda = _agenda()
    hypothesis = _hypothesis(agenda)
    assert hypothesis.feature_id in GENUINE_CORPORATE_FEATURE_IDS
    # The dataclass has no way to omit feature_id -- every call site must supply one.
    assert "feature_id" in GeneratedCorporateHypothesis.__dataclass_fields__


def test_genuine_hypothesis_cannot_use_a_control_baseline_feature() -> None:
    agenda = _agenda()
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(agenda, feature_id=CorporateFeatureId.RANDOM_NOISE_BASELINE)


def test_change_based_template_requires_a_change_eligible_feature() -> None:
    agenda = _agenda()
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(
            agenda,
            template_id="CORPORATE_CHANGE_V1",
            family=CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
            feature_id=CorporateFeatureId.OPERATING_MARGIN,
        )
    ok = _hypothesis(
        agenda,
        template_id="CORPORATE_CHANGE_V1",
        family=CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
        feature_id=CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
    )
    assert ok.template_id == "CORPORATE_CHANGE_V1"


def test_conditional_template_requires_secondary_context() -> None:
    agenda = _agenda()
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(
            agenda,
            template_id="CONDITIONAL_RELATIONSHIP_V1",
            family=CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME,
        )
    ok = _hypothesis(
        agenda,
        template_id="CONDITIONAL_RELATIONSHIP_V1",
        family=CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME,
        secondary_context_dimension="EQUITY_TREND",
        secondary_condition_label="UPTREND",
    )
    assert ok.secondary_context_dimension == "EQUITY_TREND"


def test_interaction_template_requires_secondary_context() -> None:
    agenda = _agenda()
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(
            agenda,
            template_id="INTERACTION_V1",
            family=CorporateHypothesisFamily.INTERACTION_FUNDAMENTAL_MARKET,
        )


def test_negative_control_flag_must_match_discovery_status() -> None:
    agenda = _agenda()
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(
            agenda,
            is_negative_control=True,
            negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
        )


def test_negative_control_requires_control_family_and_kind() -> None:
    agenda = _agenda()
    control = _hypothesis(
        agenda,
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
    )
    assert control.is_negative_control is True
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(
            agenda,
            family=CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
            is_negative_control=True,
            negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
            discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
        )


def test_assisted_proposals_are_disabled() -> None:
    agenda = _agenda()
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(agenda, proposal_source=ProposalSource.ASSISTED_MODEL_SEAM)


def test_hypothesis_must_stay_within_agenda_allow_lists() -> None:
    agenda = _agenda(allowed_horizons_sessions=(5,))
    with pytest.raises(CorporateMarketResearchError):
        _hypothesis(agenda, horizon_sessions=20)


# ==========================================================================
# Panel-dependence diagnostics (section 15): shared-market-date clustering,
# effective sample, inference suppression.
# ==========================================================================


def test_panel_dependence_uses_distinct_date_count_as_effective_sample() -> None:
    rows = tuple(
        CorporatePanelRow(stable_security_id=f"SEC_{i}", session_date=date, value=Decimal("0.01"))
        for date in ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08")
        for i in range(10)
    )
    diagnostics = compute_panel_dependence_diagnostics(rows)
    assert diagnostics.raw_sample_count == 50
    assert diagnostics.effective_sample_count == Decimal(5)
    assert diagnostics.overlapping_pair_count == 5
    assert "PANEL_SHARED_MARKET_DATE_DEPENDENCE" in diagnostics.note


def test_panel_dependence_suppresses_inference_below_minimum_clusters() -> None:
    rows = tuple(
        CorporatePanelRow(stable_security_id=f"SEC_{i}", session_date=date, value=Decimal("0.01"))
        for date in ("2024-01-02", "2024-01-03")
        for i in range(20)
    )
    diagnostics = compute_panel_dependence_diagnostics(rows)
    assert diagnostics.inference_available is False
    assert diagnostics.handling_mode is OverlapHandlingMode.EXCLUDED_FROM_INFERENCE
    assert "INSUFFICIENT_DISTINCT_DATES" in diagnostics.note


def test_panel_dependence_retains_descriptive_stats_for_empty_sample() -> None:
    diagnostics = compute_panel_dependence_diagnostics(())
    assert diagnostics.raw_sample_count == 0
    assert diagnostics.inference_available is False


def test_panel_dependence_sufficient_clusters_allow_inference() -> None:
    dates = tuple(f"2024-01-{day:02d}" for day in range(2, 2 + 10))
    rows = tuple(
        CorporatePanelRow(stable_security_id="SEC_A", session_date=date, value=Decimal("0.01"))
        for date in dates
    )
    diagnostics = compute_panel_dependence_diagnostics(rows)
    assert diagnostics.inference_available is True
    assert diagnostics.effective_sample_count == Decimal(10)


# ==========================================================================
# Section 6/22: outcome computation helpers used directly by the domain
# module (sector-relative return, pearson correlation for the bounded
# redundancy check).
# ==========================================================================


def test_sector_relative_return_is_difference_of_two_returns() -> None:
    value = sector_relative_return(
        security_return=Decimal("0.10"), sector_basket_return=Decimal("0.04")
    )
    assert value == Decimal("0.06000000")


def test_sector_relative_return_is_none_when_either_side_missing() -> None:
    missing_security = sector_relative_return(
        security_return=None, sector_basket_return=Decimal("0.01")
    )
    missing_sector = sector_relative_return(
        security_return=Decimal("0.01"), sector_basket_return=None
    )
    assert missing_security is None
    assert missing_sector is None


def test_pearson_correlation_perfect_positive() -> None:
    pairs = tuple((Decimal(i), Decimal(2 * i)) for i in range(1, 6))
    correlation = pearson_correlation(pairs)
    assert correlation == Decimal("1.00000000")


def test_pearson_correlation_requires_minimum_sample() -> None:
    assert pearson_correlation(((Decimal(1), Decimal(1)), (Decimal(2), Decimal(2)))) is None


def test_pearson_correlation_none_for_zero_variance() -> None:
    pairs = tuple((Decimal(1), Decimal(i)) for i in range(5))
    assert pearson_correlation(pairs) is None


# ==========================================================================
# Section 8/22: incremental-contribution and contribution-accounting
# records never collapse standalone/incremental effects into one score.
# ==========================================================================


def test_incremental_contribution_result_flags_when_combined_exceeds_market_only() -> None:
    result = IncrementalContributionResult.create(
        hypothesis_id="hyp-1",
        feature_id=CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        context_dimension="BOND_RISK_RELATIONSHIP",
        market_only_effect=Decimal("0.01"),
        corporate_only_effect=Decimal("0.02"),
        combined_effect=Decimal("0.05"),
        market_only_sample=40,
        corporate_only_sample=40,
        combined_sample=20,
    )
    assert result.incremental_delta_present is True
    assert result.market_only_effect == Decimal("0.01")
    assert result.corporate_only_effect == Decimal("0.02")
    assert result.combined_effect == Decimal("0.05")


def test_incremental_contribution_result_handles_unavailable_effects() -> None:
    result = IncrementalContributionResult.create(
        hypothesis_id="hyp-2",
        feature_id=CorporateFeatureId.CASH_TO_ASSETS,
        context_dimension="EQUITY_RISK_TONE",
        market_only_effect=None,
        corporate_only_effect=None,
        combined_effect=None,
        market_only_sample=0,
        corporate_only_sample=0,
        combined_sample=0,
    )
    assert result.incremental_delta_present is False


def test_contribution_evidence_retains_redundancy_and_coverage_separately() -> None:
    evidence = CorporateContributionEvidence.create(
        hypothesis_id="hyp-3",
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        standalone_effect_size=Decimal("0.30"),
        incremental_result_id="incremental-1",
        redundant_with=((CorporateFeatureId.NET_INCOME_GROWTH_YOY, Decimal("0.62")),),
        coverage_fraction=Decimal("0.85"),
        missingness_burden_count=6,
        notes=("NOTE_A",),
    )
    assert evidence.standalone_effect_size == Decimal("0.30")
    assert evidence.redundant_with[0][0] is CorporateFeatureId.NET_INCOME_GROWTH_YOY
    assert evidence.coverage_fraction == Decimal("0.85")
    assert evidence.missingness_burden_count == 6


def test_contribution_evidence_rejects_out_of_bounds_coverage() -> None:
    with pytest.raises(CorporateMarketResearchError):
        CorporateContributionEvidence.create(
            hypothesis_id="hyp-4",
            feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
            standalone_effect_size=None,
            incremental_result_id=None,
            redundant_with=(),
            coverage_fraction=Decimal("1.20"),
            missingness_burden_count=0,
            notes=(),
        )


# ==========================================================================
# Section 23/24: forward options seam -- structurally ready, never
# attached to a historical frame.
# ==========================================================================


def test_options_seam_unavailable_state_carries_no_descriptive_values() -> None:
    snapshot = OptionsForwardSeamSnapshot.unavailable(reason="ZERO_GENUINE_FORWARD_SESSIONS")
    assert snapshot.availability == "OPTIONS_SEAM_FACTUALLY_UNAVAILABLE_ZERO_FORWARD_SESSIONS"
    assert snapshot.as_of is None
    assert snapshot.underlying_symbols == ()
    assert snapshot.atm_iv_by_underlying == ()
    assert "ZERO_GENUINE_FORWARD_SESSIONS" in snapshot.limitations
