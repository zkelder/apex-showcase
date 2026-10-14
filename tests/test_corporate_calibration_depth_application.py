"""Application-level proofs for Corporate Research Calibration Depth v1:
strata-inventory construction over the real v3 hypothesis catalog, the ten
corporate null-generation mechanisms (issuer derangement, temporal
displacement, concept substitution, surrogate distribution, filing-event
date matching, conditional/interaction outcome nulling), panel-dependence
parity with genuine hypotheses, the full plan -> run -> finalize ->
reassess -> requalify pipeline, and CLI interface wiring -- driven by a
small, fully synthetic, hand-built ``CorporatePanel`` (no database, no
provider access), mirroring ``tests/test_corporate_market_research_
application.py``'s own offline-fixture convention.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.corporate_calibration_depth import (
    CONTROLS_PER_STRATUM,
    AdvanceCorporateCalibrationRun,
    FinalizeCorporateCalibration,
    InMemoryCorporateCalibrationDepthRepository,
    RegisterCorporateCalibrationPlan,
    RegisterCorporateCalibrationRun,
    RequalifyCorporateFollowUps,
    _seeded_derangement,
    _seeded_displaced_date,
    build_corporate_null_sample,
    build_corporate_strata_inventory,
    generate_corporate_null_control_specs,
)
from apex.application.corporate_market_research import (
    BROAD_MARKET_SYMBOL,
    AdvanceCorporateAgendaRun,
    CorporateContextDimension,
    CorporatePanel,
    InMemoryCorporateMarketResearchRepository,
    RegisterCorporateAgendaRun,
    TradingCalendar,
    build_filing_event_observations,
    corporate_matched_stratum_key,
    split_train_validation_holdout,
)
from apex.application.research_methodology import InMemoryResearchMethodologyRepository
from apex.domain.corporate_fundamentals import CorporateFilingEvent, FilingForm
from apex.domain.corporate_market_research import (
    ALLOWED_HORIZONS_SESSIONS,
    CorporateFeatureId,
    CorporateFeatureObservation,
    CorporateMarketResearchAgenda,
    CorporateOutcomeId,
)
from apex.domain.historical_replay import ResearchPeriodPolicy, ResearchPeriodRole
from apex.domain.research_calibration_v2 import ConditioningShape, ResearchDesign
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

NOW = datetime(2026, 8, 9, tzinfo=UTC)
REPLAY_RUN_ID = "calibration-depth-replay-run-test"

HIGH_SECURITIES = ("SEC_A", "SEC_B", "SEC_C")
LOW_SECURITIES = ("SEC_D", "SEC_E", "SEC_F")
ALL_SECURITIES = HIGH_SECURITIES + LOW_SECURITIES
SECTOR_BY_SECURITY = {
    "SEC_A": "TECHNOLOGY",
    "SEC_B": "TECHNOLOGY",
    "SEC_C": "FINANCIALS",
    "SEC_D": "TECHNOLOGY",
    "SEC_E": "FINANCIALS",
    "SEC_F": "FINANCIALS",
}
DAILY_RETURN_BY_SECURITY = {
    **{s: Decimal("0.0020") for s in HIGH_SECURITIES},
    **{s: Decimal("-0.0010") for s in LOW_SECURITIES},
}


def _agenda() -> CorporateMarketResearchAgenda:
    return CorporateMarketResearchAgenda.create(
        version="V3",
        objective="Calibration-depth test objective.",
        corporate_dataset_id="corporate-dataset-1",
        equity_snapshot_id="equity-snapshot-1",
        replay_run_id=REPLAY_RUN_ID,
        cross_market_dataset_id="cross-market-dataset-1",
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
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
        train_validation_holdout_policy="70/15/15",
        multiple_testing_family_policy="BH-FDR",
        robustness_requirements=("LEAVE_ONE_PERIOD_OUT",),
        lookback_years=6,
        registered_at=NOW,
    )


def _dates(count: int) -> tuple[str, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple((start + timedelta(days=i)).date().isoformat() for i in range(count))


def _closes(daily_return: Decimal, count: int, *, jitter_seed: int = 0) -> tuple[Decimal, ...]:
    """A deterministic per-day jitter is layered onto the constant trend
    so that, unlike a pure exponential path, each date's own forward
    N-session return is genuinely distinct -- otherwise a date-block
    permutation of outcomes would be numerically unobservable (every date
    would carry the identical N-session return by construction)."""

    values = []
    price = Decimal(100)
    generator = random.Random(jitter_seed)
    for _ in range(count):
        values.append(price)
        jitter = Decimal(str(round(generator.uniform(-0.0015, 0.0015), 6)))
        price = price * (Decimal(1) + daily_return + jitter)
    return tuple(values)


def _sample_filings(security: str) -> tuple[CorporateFilingEvent, ...]:
    accepted_business_hours = datetime(2024, 3, 4, 14, 0, tzinfo=UTC)
    accepted_after_close = datetime(2024, 5, 6, 22, 0, tzinfo=UTC)
    index = ALL_SECURITIES.index(security)
    cik = f"{index + 1:010d}"
    return (
        CorporateFilingEvent.create(
            stable_security_id=security,
            cik=cik,
            accession_number=f"{cik}-24-{1:06d}",
            form=FilingForm.FORM_10Q,
            report_period="2023-12-31",
            filed_at=accepted_business_hours,
            accepted_at=accepted_business_hours,
            primary_document="doc1.htm",
            file_number=None,
        ),
        CorporateFilingEvent.create(
            stable_security_id=security,
            cik=cik,
            accession_number=f"{cik}-24-{2:06d}",
            form=FilingForm.FORM_8K,
            report_period=None,
            filed_at=accepted_after_close,
            accepted_at=accepted_after_close,
            primary_document="doc2.htm",
            file_number=None,
            items=("2.02",),
            is_xbrl=True,
        ),
    )


def build_synthetic_panel(*, calendar_length: int = 260, monthly_stride: int = 5) -> CorporatePanel:
    calendar = TradingCalendar(_dates(calendar_length))
    monthly_dates = calendar.dates[::monthly_stride]
    assert len(monthly_dates) >= 24

    aligned_closes: dict[str, tuple[Decimal, ...]] = {
        BROAD_MARKET_SYMBOL: _closes(Decimal("0.0002"), calendar_length, jitter_seed=1_000),
    }
    for index, (security, daily_return) in enumerate(DAILY_RETURN_BY_SECURITY.items()):
        aligned_closes[security] = _closes(daily_return, calendar_length, jitter_seed=index + 1)

    features_by_security: dict[str, tuple[CorporateFeatureObservation, ...]] = {}
    for security in ALL_SECURITIES:
        is_high = security in HIGH_SECURITIES
        revenue_growth = Decimal("0.25") if is_high else Decimal("-0.05")
        margin_change = Decimal("0.03") if is_high else Decimal("-0.02")
        leverage = Decimal("0.30") if is_high else Decimal("0.70")
        cash_to_assets = Decimal("0.40") if is_high else Decimal("0.05")
        observations = []
        for as_of_date in monthly_dates:
            values = (
                (CorporateFeatureId.REVENUE_GROWTH_YOY, revenue_growth),
                (CorporateFeatureId.NET_INCOME_GROWTH_YOY, revenue_growth),
                (CorporateFeatureId.OPERATING_CASH_FLOW_GROWTH_YOY, revenue_growth),
                (CorporateFeatureId.OPERATING_MARGIN, Decimal("0.15")),
                (CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY, margin_change),
                (CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS, leverage),
                (CorporateFeatureId.CASH_TO_ASSETS, cash_to_assets),
                (CorporateFeatureId.SHARE_COUNT_CHANGE_YOY, Decimal("0.01")),
                (CorporateFeatureId.DAYS_SINCE_10Q_OR_10K, Decimal(45)),
                (CorporateFeatureId.RECENT_8K_INDICATOR, Decimal(0)),
                (CorporateFeatureId.AMENDMENT_INDICATOR, Decimal(0)),
                (CorporateFeatureId.FILING_LAG_FROM_PERIOD_END_DAYS, Decimal(30)),
            )
            observations.append(
                CorporateFeatureObservation(
                    stable_security_id=security,
                    session_date=as_of_date,
                    as_of=datetime.fromisoformat(as_of_date).replace(tzinfo=UTC),
                    values=values,
                    snapshot_quality="USABLE",
                    missing_fields=(),
                    fact_lineage=(),
                )
            )
        features_by_security[security] = tuple(observations)

    context_labels: dict[str, dict[str, str]] = {
        CorporateContextDimension.EQUITY_TREND: {},
        CorporateContextDimension.EQUITY_VOLATILITY: {},
        CorporateContextDimension.EQUITY_RISK_TONE: {},
        CorporateContextDimension.BOND_RISK_RELATIONSHIP: {},
    }
    for index, date_str in enumerate(monthly_dates):
        context_labels[CorporateContextDimension.EQUITY_TREND][date_str] = (
            "UPTREND" if index % 2 == 0 else "RANGE_BOUND"
        )
        context_labels[CorporateContextDimension.EQUITY_VOLATILITY][date_str] = (
            "ELEVATED_VOLATILITY" if index % 3 == 0 else "NORMAL_VOLATILITY"
        )
        context_labels[CorporateContextDimension.EQUITY_RISK_TONE][date_str] = (
            "RISK_OFF" if index % 2 == 0 else "RISK_ON"
        )
        context_labels[CorporateContextDimension.BOND_RISK_RELATIONSHIP][date_str] = (
            "RATE_HEADWIND" if index % 2 == 0 else "RATE_TAILWIND"
        )

    filing_events_by_security = {
        security: build_filing_event_observations(
            stable_security_id=security, filings=_sample_filings(security), calendar=calendar
        )
        for security in ALL_SECURITIES
    }

    return CorporatePanel(
        calendar=calendar,
        monthly_dates=monthly_dates,
        corporate_dataset_id="corporate-dataset-1",
        replay_run_id=REPLAY_RUN_ID,
        cross_market_dataset_id="cross-market-dataset-1",
        usable_security_ids=ALL_SECURITIES,
        sector_by_security=tuple(SECTOR_BY_SECURITY.items()),
        features_by_security=features_by_security,
        aligned_closes_by_security=aligned_closes,
        context_labels=context_labels,
        filing_events_by_security=filing_events_by_security,
        excluded_low_quality_security_ids=(),
        warnings=(),
    )


PANEL = build_synthetic_panel()
_period_policy = split_train_validation_holdout(PANEL.monthly_dates, replay_run_id=REPLAY_RUN_ID)
assert _period_policy is not None
PERIOD_POLICY: ResearchPeriodPolicy = _period_policy


class _FakePanelBuilder:
    def execute(self, **_kwargs: object) -> CorporatePanel:
        return PANEL


def _run_v3_agenda_in_memory() -> tuple[
    InMemoryCorporateMarketResearchRepository,
    InMemoryResearchMethodologyRepository,
    CorporateMarketResearchAgenda,
]:
    agenda = _agenda()
    repository = InMemoryCorporateMarketResearchRepository()
    repository.save_agenda(agenda)
    methodology = InMemoryResearchMethodologyRepository()
    RegisterCorporateAgendaRun(repository=repository).execute(agenda=agenda, created_at=NOW)
    advancer = AdvanceCorporateAgendaRun(
        repository=repository, methodology=methodology, panel_builder=_FakePanelBuilder()
    )
    for _step in range(50):
        result = advancer.execute(agenda=agenda, created_at=NOW)
        if result.run.status.value == "COMPLETE":
            break
    else:
        raise AssertionError("v3 agenda fixture did not reach COMPLETE")
    return repository, methodology, agenda


# ==========================================================================
# Section 2: strata inventory over the real v3 catalog
# ==========================================================================


def test_strata_inventory_maps_every_genuine_v3_hypothesis_to_exactly_one_stratum() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    hypotheses = repository.hypotheses_for_agenda(agenda.agenda_id)
    genuine = [item for item in hypotheses if not item.is_negative_control]
    controls = [item for item in hypotheses if item.is_negative_control]
    executions = repository.executions_for_agenda(agenda.agenda_id)
    trials_by_hypothesis = {}
    for execution in executions:
        trial = (
            methodology.trial_by_id(execution.validation_trial_id)
            if execution.validation_trial_id
            else None
        )
        trials_by_hypothesis[execution.hypothesis_id] = trial

    strata = build_corporate_strata_inventory(
        genuine_hypotheses=genuine,
        control_hypotheses=controls,
        trials_by_hypothesis=trials_by_hypothesis,
    )
    covered_hypothesis_ids = {hid for target in strata for hid in target.genuine_hypothesis_ids}
    assert covered_hypothesis_ids == {item.hypothesis_id for item in genuine}
    # Every genuine hypothesis's own computed stratum key matches the target it was placed in.
    target_by_stratum = {target.matched_stratum: target for target in strata}
    for hypothesis in genuine:
        stratum_key = corporate_matched_stratum_key(hypothesis)
        assert hypothesis.hypothesis_id in target_by_stratum[stratum_key].genuine_hypothesis_ids


def test_strata_inventory_distinguishes_sufficient_from_insufficient_strata() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    hypotheses = repository.hypotheses_for_agenda(agenda.agenda_id)
    genuine = [item for item in hypotheses if not item.is_negative_control]
    controls = [item for item in hypotheses if item.is_negative_control]
    executions = repository.executions_for_agenda(agenda.agenda_id)
    trials_by_hypothesis = {}
    for execution in executions:
        trial = (
            methodology.trial_by_id(execution.validation_trial_id)
            if execution.validation_trial_id
            else None
        )
        trials_by_hypothesis[execution.hypothesis_id] = trial

    strata = build_corporate_strata_inventory(
        genuine_hypotheses=genuine,
        control_hypotheses=controls,
        trials_by_hypothesis=trials_by_hypothesis,
    )
    # The cross-sectional H20 SUBSEQUENT_RETURN stratum backs 5 genuine v3 hypotheses and is
    # the only one carrying 15-16 original controls (the other 16 negative controls); every
    # other stratum in the real v3 catalog is originally insufficient.
    sufficient = [item for item in strata if item.original_sufficient]
    insufficient = [item for item in strata if not item.original_sufficient]
    assert len(sufficient) >= 1
    assert len(insufficient) >= 1
    assert len(sufficient) + len(insufficient) == len(strata) == 10


def test_strata_inventory_rejects_an_incompatible_control_silently_via_stratum_mismatch() -> None:
    """A control whose own stratum key never matches any genuine target is
    simply never counted toward that target's original_matched_null_count
    -- proving incompatible controls are excluded, not accidentally pooled."""

    repository, _methodology, agenda = _run_v3_agenda_in_memory()
    hypotheses = repository.hypotheses_for_agenda(agenda.agenda_id)
    genuine = [item for item in hypotheses if not item.is_negative_control]
    controls = [item for item in hypotheses if item.is_negative_control]
    trials_by_hypothesis = {item.hypothesis_id: None for item in controls}
    strata = build_corporate_strata_inventory(
        genuine_hypotheses=genuine,
        control_hypotheses=controls,
        trials_by_hypothesis=trials_by_hypothesis,
    )
    assert all(target.original_matched_null_count == 0 for target in strata)


# ==========================================================================
# Section 10/11: plan + spec generation
# ==========================================================================


def test_generate_specs_is_deterministic_and_matches_the_declared_budget() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    calib_repo = InMemoryCorporateCalibrationDepthRepository()
    plan = RegisterCorporateCalibrationPlan(
        repository=calib_repo,
        corporate_market_repository=repository,
        methodology=methodology,
    ).execute(agenda_id=agenda.agenda_id, registered_at=NOW)

    first = generate_corporate_null_control_specs(plan, created_at=NOW)
    second = generate_corporate_null_control_specs(plan, created_at=NOW)
    assert [item.spec_id for item in first] == [item.spec_id for item in second]
    assert (
        len(first)
        == plan.total_new_control_budget
        == CONTROLS_PER_STRATUM * len(plan.stratum_targets)
    )
    for target in plan.stratum_targets:
        count = sum(1 for item in first if item.matched_stratum == target.matched_stratum)
        assert count == CONTROLS_PER_STRATUM


def test_generate_specs_only_uses_mechanisms_valid_for_each_strata_design() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    calib_repo = InMemoryCorporateCalibrationDepthRepository()
    plan = RegisterCorporateCalibrationPlan(
        repository=calib_repo,
        corporate_market_repository=repository,
        methodology=methodology,
    ).execute(agenda_id=agenda.agenda_id, registered_at=NOW)
    specs = generate_corporate_null_control_specs(plan, created_at=NOW)
    for spec in specs:
        target = plan.stratum(spec.matched_stratum)
        assert target is not None
        assert spec.mechanism in target.mechanisms
        assert spec.research_design is target.research_design


# ==========================================================================
# Sections 5/6: cross-sectional/change-based null mechanisms
# ==========================================================================


def test_seeded_derangement_is_fixed_point_free_and_deterministic() -> None:
    mapping = _seeded_derangement(ALL_SECURITIES, seed=42)
    assert set(mapping) == set(ALL_SECURITIES)
    assert all(key != value for key, value in mapping.items())
    assert _seeded_derangement(ALL_SECURITIES, seed=42) == mapping
    assert _seeded_derangement(ALL_SECURITIES, seed=43) != mapping


def test_seeded_displaced_date_moves_strictly_backward_in_the_monthly_calendar() -> None:
    date = PANEL.monthly_dates[10]
    displaced = _seeded_displaced_date(PANEL, date, steps=3)
    assert displaced == PANEL.monthly_dates[7]
    assert displaced < date


def test_seeded_displaced_date_is_a_no_op_too_close_to_the_start_of_history() -> None:
    date = PANEL.monthly_dates[1]
    assert _seeded_displaced_date(PANEL, date, steps=5) == date


def _cross_sectional_spec(mechanism, *, seed: int = 20260809999):
    from apex.domain.corporate_calibration_depth import CorporateNullControlSpec

    return CorporateNullControlSpec.create(
        calibration_id="calibration-1",
        matched_stratum="CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|NUMERIC_SPLIT",
        research_design=ResearchDesign.CROSS_SECTIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        template_id="CROSS_SECTIONAL_V1",
        mechanism=mechanism,
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        seed=seed,
        preserved_properties=("MARKET_DATE_STRUCTURE",),
        destroyed_relationship="X",
        rationale="Test.",
        ordinal=1,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    "mechanism",
    [
        "SEEDED_TEMPORAL_DISPLACEMENT",
        "SEEDED_ISSUER_DERANGEMENT",
        "SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION",
        "SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION",
    ],
)
def test_cross_sectional_mechanisms_produce_a_nonempty_sample_on_the_synthetic_panel(
    mechanism: str,
) -> None:
    from apex.domain.corporate_calibration_depth import CorporateNullMechanism

    spec = _cross_sectional_spec(CorporateNullMechanism(mechanism))
    sample = build_corporate_null_sample(
        spec, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert sample.treatment
    assert sample.benchmark
    # Every treatment row's outcome is a genuine value read from the real, untransformed panel
    # -- only the feature side is ever perturbed by these four mechanisms.
    for row in sample.treatment:
        assert isinstance(row.value, Decimal)


def test_cross_sectional_mechanisms_are_deterministic_given_the_same_seed() -> None:
    from apex.domain.corporate_calibration_depth import CorporateNullMechanism

    spec_a = _cross_sectional_spec(CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT, seed=7)
    spec_b = _cross_sectional_spec(CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT, seed=7)
    sample_a = build_corporate_null_sample(
        spec_a, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    sample_b = build_corporate_null_sample(
        spec_b, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample_a == sample_b


def test_cross_sectional_mechanisms_differ_across_seeds() -> None:
    from apex.domain.corporate_calibration_depth import CorporateNullMechanism

    spec_a = _cross_sectional_spec(CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT, seed=7)
    spec_b = _cross_sectional_spec(CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT, seed=99)
    sample_a = build_corporate_null_sample(
        spec_a, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    sample_b = build_corporate_null_sample(
        spec_b, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample_a != sample_b


# ==========================================================================
# Section 7: filing-event null mechanisms
# ==========================================================================


@pytest.mark.parametrize(
    "mechanism",
    [
        "SEEDED_RANDOM_NON_EVENT_DATE_MATCHED",
        "SEEDED_DISPLACED_FILING_EVENT_DATE",
        "SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT",
    ],
)
def test_filing_event_mechanisms_produce_a_random_date_baseline_sample(mechanism: str) -> None:
    from apex.domain.corporate_calibration_depth import (
        CorporateNullControlSpec,
        CorporateNullMechanism,
    )
    from apex.domain.research_methodology import BenchmarkKind

    spec = CorporateNullControlSpec.create(
        calibration_id="calibration-1",
        matched_stratum="FILING_EVENT|SUBSEQUENT_RETURN|H5|UNCONDITIONAL",
        research_design=ResearchDesign.FILING_EVENT,
        conditioning_shape=ConditioningShape.UNCONDITIONAL,
        template_id="FILING_EVENT_V1",
        mechanism=CorporateNullMechanism(mechanism),
        feature_id=CorporateFeatureId.RECENT_8K_INDICATOR,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=5,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        seed=20260809555,
        preserved_properties=("EVENT_FREQUENCY",),
        destroyed_relationship="X",
        rationale="Test.",
        ordinal=1,
        created_at=NOW,
    )
    sample = build_corporate_null_sample(
        spec, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert sample.benchmark_kind is BenchmarkKind.RANDOM_DATE_BASELINE


# ==========================================================================
# Section 8: conditional/interaction null mechanisms
# ==========================================================================


@pytest.mark.parametrize(
    "mechanism",
    [
        "SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION",
        "SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION",
        "SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION",
    ],
)
def test_conditional_mechanisms_preserve_the_real_conditioning_group_membership(
    mechanism: str,
) -> None:
    """Section 8: every sampled date must genuinely satisfy the declared
    secondary condition (EQUITY_TREND=UPTREND) -- the real conditioning
    structure is preserved even though the outcome pairing is nulled."""

    from apex.domain.corporate_calibration_depth import (
        CorporateNullControlSpec,
        CorporateNullMechanism,
    )

    spec = CorporateNullControlSpec.create(
        calibration_id="calibration-1",
        matched_stratum="CONDITIONAL|SUBSEQUENT_RETURN|H20|LABEL_SPLIT",
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.LABEL_SPLIT,
        template_id="CONDITIONAL_RELATIONSHIP_V1",
        mechanism=CorporateNullMechanism(mechanism),
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        secondary_context_dimension=CorporateContextDimension.EQUITY_TREND,
        secondary_condition_label="UPTREND",
        seed=20260809777,
        preserved_properties=("CONDITIONING_GROUP_FREQUENCIES",),
        destroyed_relationship="X",
        rationale="Test.",
        ordinal=1,
        created_at=NOW,
    )
    sample = build_corporate_null_sample(
        spec, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert sample.benchmark
    for row in sample.benchmark:
        assert (
            PANEL.context_label(CorporateContextDimension.EQUITY_TREND, row.session_date)
            == "UPTREND"
        )


def test_conditional_mechanisms_destroy_the_real_feature_outcome_pairing() -> None:
    """The block-permutation mechanism substitutes a *different* date's
    outcome cross-section -- the outcome pairing must genuinely change
    versus the untransformed panel reading for at least one row (the
    fixture's per-day price jitter -- see ``_closes`` -- ensures each
    date's own N-session return is genuinely distinct, so this is
    observable end to end rather than only at the internal date map)."""

    from apex.domain.corporate_calibration_depth import (
        CorporateNullControlSpec,
        CorporateNullMechanism,
    )

    spec = CorporateNullControlSpec.create(
        calibration_id="calibration-1",
        matched_stratum="CONDITIONAL|SUBSEQUENT_RETURN|H20|LABEL_SPLIT",
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.LABEL_SPLIT,
        template_id="CONDITIONAL_RELATIONSHIP_V1",
        mechanism=CorporateNullMechanism.SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION,
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        secondary_context_dimension=CorporateContextDimension.EQUITY_TREND,
        secondary_condition_label="UPTREND",
        seed=1,
        preserved_properties=("CONDITIONING_GROUP_FREQUENCIES",),
        destroyed_relationship="X",
        rationale="Test.",
        ordinal=1,
        created_at=NOW,
    )
    sample = build_corporate_null_sample(
        spec, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert sample.benchmark
    real_values = {
        (row.stable_security_id, row.session_date): PANEL.outcome(
            stable_security_id=row.stable_security_id,
            session_date=row.session_date,
            horizon_sessions=20,
        ).outcome(CorporateOutcomeId.SUBSEQUENT_RETURN)
        for row in sample.benchmark
    }
    permuted_values = {
        (row.stable_security_id, row.session_date): row.value for row in sample.benchmark
    }
    assert permuted_values != real_values


# ==========================================================================
# Section 9: panel dependence parity with genuine hypotheses
# ==========================================================================


def test_null_sample_diagnostics_use_the_distinct_date_effective_sample() -> None:
    from apex.application.corporate_calibration_depth import build_corporate_null_trial
    from apex.domain.corporate_calibration_depth import CorporateNullMechanism
    from apex.domain.research_methodology import ResearchPhase

    spec = _cross_sectional_spec(CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT)
    sample = build_corporate_null_sample(
        spec, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    trial = build_corporate_null_trial(
        spec,
        sample,
        dataset_snapshot_id="d",
        replay_run_id="r",
        phase=ResearchPhase.VALIDATION,
        governance_policy=None,
        created_at=NOW,
    )
    assert trial is not None
    distinct_dates = len({row.session_date for row in sample.treatment})
    assert trial.diagnostics.overlap.effective_sample_count == Decimal(distinct_dates)
    assert trial.diagnostics.overlap.effective_sample_count <= Decimal(
        trial.diagnostics.overlap.raw_sample_count
    )


# ==========================================================================
# Full pipeline: plan -> run -> finalize -> reassess -> requalify
# ==========================================================================


def test_full_calibration_depth_pipeline_runs_to_completion_without_a_database() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    calib_repo = InMemoryCorporateCalibrationDepthRepository()
    plan = RegisterCorporateCalibrationPlan(
        repository=calib_repo,
        corporate_market_repository=repository,
        methodology=methodology,
    ).execute(agenda_id=agenda.agenda_id, registered_at=NOW)
    run_result = RegisterCorporateCalibrationRun(repository=calib_repo).execute(
        plan=plan, created_at=NOW
    )
    advancer = AdvanceCorporateCalibrationRun(
        repository=calib_repo,
        methodology=methodology,
        panel=PANEL,
        period_policy=PERIOD_POLICY,
    )
    last_result = None
    for _step in range(len(run_result.specs) + 2):
        last_result = advancer.execute(plan=plan, created_at=NOW)
        if last_result.run.status.value == "COMPLETE":
            break
    assert last_result is not None
    assert last_result.run.status.value == "COMPLETE"
    assert len(last_result.run.completed_spec_ids) == len(run_result.specs)
    assert len(set(last_result.run.completed_spec_ids)) == len(run_result.specs)  # no duplicates

    final_run = calib_repo.run_by_calibration(plan.calibration_id)
    assert final_run is not None
    finalizer = FinalizeCorporateCalibration(
        repository=calib_repo,
        corporate_market_repository=repository,
        methodology=methodology,
        panel=PANEL,
        period_policy=PERIOD_POLICY,
    )
    report = finalizer.execute(plan=plan, run=final_run, created_at=NOW)
    assert report.strata_evaluated == 10
    assert report.controls_generated == len(run_result.specs)
    assert report.genuine_reassessment_count == 20
    assert report.control_reassessment_count == 17
    assert 0 <= report.raw_false_positive_rate <= 1
    assert 0 <= report.corrected_false_positive_rate <= 1
    assert report.corrected_false_positive_rate <= report.raw_false_positive_rate

    reassessments = calib_repo.reassessments_for_calibration(plan.calibration_id)
    assert len(reassessments) == 37

    qualifications = RequalifyCorporateFollowUps(
        repository=calib_repo,
        corporate_market_repository=repository,
    ).execute(plan=plan, reassessments=reassessments, created_at=NOW)
    assert len(qualifications) == len(repository.list_follow_ups())
    for item in qualifications:
        assert item.calibration_id == plan.calibration_id


def test_advancing_a_complete_calibration_run_is_a_genuine_no_op() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    calib_repo = InMemoryCorporateCalibrationDepthRepository()
    plan = RegisterCorporateCalibrationPlan(
        repository=calib_repo,
        corporate_market_repository=repository,
        methodology=methodology,
    ).execute(agenda_id=agenda.agenda_id, registered_at=NOW)
    run_result = RegisterCorporateCalibrationRun(repository=calib_repo).execute(
        plan=plan, created_at=NOW
    )
    advancer = AdvanceCorporateCalibrationRun(
        repository=calib_repo,
        methodology=methodology,
        panel=PANEL,
        period_policy=PERIOD_POLICY,
    )
    for _step in range(len(run_result.specs) + 2):
        result = advancer.execute(plan=plan, created_at=NOW)
        if result.run.status.value == "COMPLETE":
            break
    before = calib_repo.run_by_calibration(plan.calibration_id)
    again = advancer.execute(plan=plan, created_at=NOW)
    after = calib_repo.run_by_calibration(plan.calibration_id)
    assert before == after
    assert again.outcome is None


def test_registering_the_same_agenda_twice_returns_the_identical_plan() -> None:
    repository, methodology, agenda = _run_v3_agenda_in_memory()
    calib_repo = InMemoryCorporateCalibrationDepthRepository()
    registrar = RegisterCorporateCalibrationPlan(
        repository=calib_repo,
        corporate_market_repository=repository,
        methodology=methodology,
    )
    first = registrar.execute(agenda_id=agenda.agenda_id, registered_at=NOW)
    second = registrar.execute(agenda_id=agenda.agenda_id, registered_at=NOW)
    assert first.calibration_id == second.calibration_id
    assert len(calib_repo.list_plans()) == 1


# ==========================================================================
# Interfaces: CLI parsing and dispatch (bounded, authority NONE)
# ==========================================================================


def test_cli_parses_every_corporate_calibration_depth_command() -> None:
    import scripts.dev as dev

    parser = dev.parser()
    for command, extra in (
        ("corporate-calibration-create", ["--created-at", "2026-08-09T00:00:00Z"]),
        ("corporate-calibration-inspect", []),
        ("corporate-calibration-strata-inspect", []),
        ("corporate-calibration-run-start", ["--created-at", "2026-08-09T00:00:00Z"]),
        ("corporate-calibration-run-resume", ["--created-at", "2026-08-09T00:00:00Z"]),
        ("corporate-calibration-run-inspect", []),
        ("corporate-market-recalibrate", []),
        ("corporate-calibration-report-inspect", []),
        ("corporate-followup-requalify", []),
    ):
        parsed = parser.parse_args([command, *extra])
        assert parsed.command == command


def test_cli_dispatch_routes_calibration_depth_commands_before_the_market_research_prefix() -> None:
    import inspect

    import scripts.dev as dev

    source = inspect.getsource(dev.corporate_fundamentals_command)
    calibration_index = source.index('startswith("corporate-calibration-")')
    market_index = source.index('startswith("corporate-market-"):')
    assert calibration_index < market_index


def test_cli_dispatch_does_not_collide_with_the_outer_corporate_prefix_guard() -> None:
    import inspect

    import scripts.dev as dev

    source = inspect.getsource(dev.main)
    dispatch_index = source.index('if arguments.command.startswith("corporate-"):')
    preceding = source[:dispatch_index]
    assert 'startswith("corporate' not in preceding
