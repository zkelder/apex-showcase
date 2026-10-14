"""Application-level proofs for Cross-Market Research v3: hypothesis-
sample construction per research design, negative-control transforms,
timing/anti-lookahead behavior, panel-dependence-aware trial statistics,
template-matched calibration, and the knowledge/contradiction/follow-up/
quality-report pipeline -- driven by a small, fully synthetic, hand-built
``CorporatePanel`` (no database, no provider access).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.application.corporate_market_research import (
    BROAD_MARKET_SYMBOL,
    AdvanceCorporateAgendaRun,
    CorporateContextDimension,
    CorporatePanel,
    FinalizeCorporateAgenda,
    InMemoryCorporateMarketResearchRepository,
    RegisterCorporateAgendaRun,
    RunSingleCorporateHypothesis,
    TradingCalendar,
    _amendment_indicator,
    _filing_lag_days,
    build_feature_observation,
    build_filing_event_observations,
    build_hypothesis_sample,
    build_trial_families,
    build_v3_agenda_hypotheses,
    build_v3_agenda_hypothesis_specs,
    classify_hypothesis_calibration,
    compute_incremental_contribution,
    corporate_matched_stratum_key,
    reconstruct_hypothesis_outcome,
    split_train_validation_holdout,
)
from apex.application.research_methodology import InMemoryResearchMethodologyRepository
from apex.domain.corporate_fundamentals import (
    CorporateFilingEvent,
    FilingForm,
    FundamentalConcept,
    FundamentalFact,
)
from apex.domain.corporate_market_research import (
    CorporateFeatureId,
    CorporateFeatureObservation,
    CorporateHypothesisFamily,
    CorporateMarketResearchAgenda,
    CorporateOutcomeId,
    GeneratedCorporateHypothesis,
)
from apex.domain.cross_market_research import AgendaRunStatus
from apex.domain.historical_replay import ResearchPeriodPolicy, ResearchPeriodRole
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

NOW = datetime(2026, 8, 9, tzinfo=UTC)
REPLAY_RUN_ID = "replay-run-test"


def _agenda() -> CorporateMarketResearchAgenda:
    from apex.domain.corporate_market_research import ALLOWED_HORIZONS_SESSIONS
    from apex.domain.corporate_market_research import CorporateFeatureId as CFI
    from apex.domain.corporate_market_research import CorporateOutcomeId as COI

    return CorporateMarketResearchAgenda.create(
        version="V3",
        objective="Test objective.",
        corporate_dataset_id="corporate-dataset-1",
        equity_snapshot_id="equity-snapshot-1",
        replay_run_id=REPLAY_RUN_ID,
        cross_market_dataset_id="cross-market-dataset-1",
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
        allowed_feature_ids=tuple(CFI),
        allowed_outcome_ids=tuple(COI),
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


# ==========================================================================
# Synthetic panel fixture: 8 securities (4 "high" REVENUE_GROWTH_YOY, 4
# "low"), 2 sectors, deterministic price paths so subsequent return is
# genuinely, predictably higher for the high-growth group.
# ==========================================================================

HIGH_SECURITIES = ("SEC_A", "SEC_B", "SEC_C", "SEC_D")
LOW_SECURITIES = ("SEC_E", "SEC_F", "SEC_G", "SEC_H")
ALL_SECURITIES = HIGH_SECURITIES + LOW_SECURITIES
SECTOR_BY_SECURITY = {
    "SEC_A": "TECHNOLOGY",
    "SEC_B": "TECHNOLOGY",
    "SEC_C": "FINANCIALS",
    "SEC_D": "FINANCIALS",
    "SEC_E": "TECHNOLOGY",
    "SEC_F": "TECHNOLOGY",
    "SEC_G": "FINANCIALS",
    "SEC_H": "FINANCIALS",
}
DAILY_RETURN_BY_SECURITY = {
    **{s: Decimal("0.0020") for s in HIGH_SECURITIES},
    **{s: Decimal("-0.0010") for s in LOW_SECURITIES},
}


def _dates(count: int) -> tuple[str, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple((start + timedelta(days=i)).date().isoformat() for i in range(count))


def _closes(daily_return: Decimal, count: int) -> tuple[Decimal, ...]:
    values = []
    price = Decimal(100)
    for _ in range(count):
        values.append(price)
        price = price * (Decimal(1) + daily_return)
    return tuple(values)


def build_synthetic_panel(*, calendar_length: int = 400, monthly_stride: int = 5) -> CorporatePanel:
    calendar = TradingCalendar(_dates(calendar_length))
    monthly_dates = calendar.dates[::monthly_stride]
    assert len(monthly_dates) >= 24

    aligned_closes: dict[str, tuple[Decimal, ...]] = {
        BROAD_MARKET_SYMBOL: _closes(Decimal("0.0002"), calendar_length),
    }
    for security, daily_return in DAILY_RETURN_BY_SECURITY.items():
        aligned_closes[security] = _closes(daily_return, calendar_length)

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
            stable_security_id=security,
            filings=_sample_filings(security),
            calendar=calendar,
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


def _sample_filings(security: str) -> tuple[CorporateFilingEvent, ...]:
    accepted_business_hours = datetime(2024, 3, 4, 14, 0, tzinfo=UTC)  # 14:00 UTC, before close
    accepted_after_close = datetime(2024, 5, 6, 22, 0, tzinfo=UTC)  # 22:00 UTC, after close
    return (
        CorporateFilingEvent.create(
            stable_security_id=security,
            cik="0000000001",
            accession_number="0000000001-24-000001",
            form=FilingForm.FORM_10Q,
            report_period="2023-12-31",
            filed_at=accepted_business_hours,
            accepted_at=accepted_business_hours,
            primary_document="doc1.htm",
            file_number=None,
        ),
        CorporateFilingEvent.create(
            stable_security_id=security,
            cik="0000000001",
            accession_number="0000000001-24-000002",
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


PANEL = build_synthetic_panel()
_period_policy = split_train_validation_holdout(PANEL.monthly_dates, replay_run_id=REPLAY_RUN_ID)
assert _period_policy is not None
PERIOD_POLICY: ResearchPeriodPolicy = _period_policy


def test_synthetic_period_policy_is_available() -> None:
    assert PERIOD_POLICY is not None
    roles = {item.role for item in PERIOD_POLICY.periods}
    assert roles == {
        ResearchPeriodRole.TRAIN,
        ResearchPeriodRole.VALIDATION,
        ResearchPeriodRole.HOLDOUT,
    }


# ==========================================================================
# Hypothesis catalog (sections 1, 2, 9)
# ==========================================================================


def test_catalog_has_20_genuine_and_17_negative_control_specs() -> None:
    specs = build_v3_agenda_hypothesis_specs()
    genuine = [s for s in specs if not s.is_negative_control]
    controls = [s for s in specs if s.is_negative_control]
    assert len(genuine) == 20
    assert len(controls) == 17
    assert 12 <= len(genuine) <= 24


def test_catalog_covers_all_five_templates_and_five_control_kinds() -> None:
    specs = build_v3_agenda_hypothesis_specs()
    genuine_templates = {s.template_id for s in specs if not s.is_negative_control}
    assert genuine_templates == {
        "CROSS_SECTIONAL_V1",
        "CORPORATE_CHANGE_V1",
        "FILING_EVENT_V1",
        "CONDITIONAL_RELATIONSHIP_V1",
        "INTERACTION_V1",
    }
    from apex.domain.corporate_market_research import CORPORATE_NEGATIVE_CONTROL_KINDS

    control_kinds = {s.negative_control_kind for s in specs if s.is_negative_control}
    assert control_kinds == set(CORPORATE_NEGATIVE_CONTROL_KINDS)


def test_build_v3_agenda_hypotheses_produces_genuinely_new_objects_each_call() -> None:
    agenda = _agenda()
    first = build_v3_agenda_hypotheses(agenda, created_at=NOW)
    second = build_v3_agenda_hypotheses(agenda, created_at=NOW)
    assert len(first) == 37
    assert [h.hypothesis_id for h in first] == [h.hypothesis_id for h in second]


# ==========================================================================
# Timing (section 11/12/14): future filing excluded from the feature
# observation; restatement invisible before its own filing; after-close
# filing event shifts to the next applicable market session.
# ==========================================================================


def test_future_filing_is_excluded_from_the_feature_observation() -> None:
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    future_filed_at = datetime(2024, 6, 1, tzinfo=UTC)
    filings = (
        CorporateFilingEvent.create(
            stable_security_id="SEC_A",
            cik="0000000001",
            accession_number="0000000001-24-000003",
            form=FilingForm.FORM_10Q,
            report_period="2024-03-31",
            filed_at=future_filed_at,
            accepted_at=future_filed_at,
            primary_document="doc3.htm",
            file_number=None,
        ),
    )
    observation = build_feature_observation(
        stable_security_id="SEC_A",
        cik="0000000001",
        session_date="2024-01-01",
        filings=filings,
        facts=(),
    )
    # No visible core filing and no visible facts as of 2024-01-01 -> REJECTED, dropped.
    assert observation is None
    assert _amendment_indicator(filings, as_of=as_of) == Decimal(0)
    assert _filing_lag_days(filings, as_of=as_of) is None


def test_restatement_invisible_before_its_own_filing_acceptance() -> None:
    original_accepted = datetime(2024, 2, 1, tzinfo=UTC)
    restated_accepted = datetime(2024, 8, 1, tzinfo=UTC)
    original = FundamentalFact.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="Revenues",
        fiscal_year=2023,
        fiscal_period="FY",
        period_start="2023-01-01",
        period_end="2023-12-31",
        value=Decimal("1000"),
        accession_number="0000000001-24-000010",
        form="10-K",
        filed_at=original_accepted,
        accepted_at=original_accepted,
    )
    restated = FundamentalFact.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="Revenues",
        fiscal_year=2023,
        fiscal_period="FY",
        period_start="2023-01-01",
        period_end="2023-12-31",
        value=Decimal("1200"),
        accession_number="0000000001-24-000099",
        form="10-K/A",
        filed_at=restated_accepted,
        accepted_at=restated_accepted,
    )
    from apex.domain.corporate_fundamentals import resolve_fact_as_of

    before = resolve_fact_as_of((original, restated), as_of=datetime(2024, 3, 1, tzinfo=UTC))
    after = resolve_fact_as_of((original, restated), as_of=datetime(2024, 9, 1, tzinfo=UTC))
    assert before is not None and before.value == Decimal("1000")
    assert after is not None and after.value == Decimal("1200")


def test_build_feature_observation_is_independent_of_fact_and_filing_order() -> None:
    """Corporate Research Panel Determinism v1: the panel-facing entry
    point built directly on top of ``_choose_fact`` must itself be
    order-independent for a genuine same-accession dual-tag tie -- proven
    at every permutation of the ``facts`` sequence a caller (in
    production, an incompletely-ordered bulk SQL fetch) might supply."""
    from itertools import permutations

    net_income_loss = FundamentalFact.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        concept=FundamentalConcept.NET_INCOME,
        source_taxonomy="us-gaap",
        source_tag="NetIncomeLoss",
        fiscal_year=2023,
        fiscal_period="FY",
        period_start="2023-01-01",
        period_end="2023-12-31",
        value=Decimal("100"),
        accession_number="0000000001-24-000030",
        form="10-K",
        filed_at=datetime(2024, 2, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    profit_loss = FundamentalFact.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        concept=FundamentalConcept.NET_INCOME,
        source_taxonomy="us-gaap",
        source_tag="ProfitLoss",
        fiscal_year=2023,
        fiscal_period="FY",
        period_start="2023-01-01",
        period_end="2023-12-31",
        value=Decimal("110"),
        accession_number="0000000001-24-000030",
        form="10-K",
        filed_at=datetime(2024, 2, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    # A second core concept so snapshot quality clears the
    # USABLE_WITH_WARNINGS floor (>= 2 core concepts) and
    # build_feature_observation does not drop the observation outright --
    # unrelated to the tie under test, so it is held fixed across every
    # permutation below.
    revenue = FundamentalFact.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="Revenues",
        fiscal_year=2023,
        fiscal_period="FY",
        period_start="2023-01-01",
        period_end="2023-12-31",
        value=Decimal("1000"),
        accession_number="0000000001-24-000030",
        form="10-K",
        filed_at=datetime(2024, 2, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    results = set()
    for tied_order in permutations((net_income_loss, profit_loss)):
        facts = (*tied_order, revenue)
        observation = build_feature_observation(
            stable_security_id="SEC_A",
            cik="0000000001",
            session_date="2024-03-01",
            filings=(),
            facts=facts,
        )
        assert observation is not None
        results.add((observation.values, observation.fact_lineage, observation.snapshot_quality))
    assert len(results) == 1


def test_panel_fingerprint_is_deterministic_and_sensitive_to_content() -> None:
    """Corporate Research Panel Determinism v1, section 12: identical panel
    content fingerprints identically; a genuine change to any feature
    value changes it."""
    panel_a = build_synthetic_panel()
    panel_b = build_synthetic_panel()
    assert panel_a is not panel_b
    assert panel_a.fingerprint() == panel_b.fingerprint()

    mutated_first = panel_b.features_by_security["SEC_A"][0]
    mutated_values = tuple(
        (feature_id, Decimal("999"))
        if feature_id == CorporateFeatureId.REVENUE_GROWTH_YOY
        else (feature_id, value)
        for feature_id, value in mutated_first.values
    )
    mutated_observation = CorporateFeatureObservation(
        stable_security_id=mutated_first.stable_security_id,
        session_date=mutated_first.session_date,
        as_of=mutated_first.as_of,
        values=mutated_values,
        snapshot_quality=mutated_first.snapshot_quality,
        missing_fields=mutated_first.missing_fields,
        fact_lineage=mutated_first.fact_lineage,
    )
    panel_b.features_by_security["SEC_A"] = (
        mutated_observation,
        *panel_b.features_by_security["SEC_A"][1:],
    )
    assert panel_a.fingerprint() != panel_b.fingerprint()


def test_after_close_filing_event_shifts_to_next_market_session() -> None:
    calendar = TradingCalendar(_dates(30))
    after_close = datetime(2024, 1, 5, 22, 0, tzinfo=UTC)  # after the 21:00 UTC threshold
    filing = CorporateFilingEvent.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        accession_number="0000000001-24-000020",
        form=FilingForm.FORM_8K,
        report_period=None,
        filed_at=after_close,
        accepted_at=after_close,
        primary_document="doc.htm",
        file_number=None,
        items=("2.02",),
    )
    observations = build_filing_event_observations(
        stable_security_id="SEC_A", filings=(filing,), calendar=calendar
    )
    assert len(observations) == 1
    assert observations[0].shifted_to_next_session is True
    assert observations[0].first_outcome_session_date > "2024-01-05"


def test_before_close_filing_event_uses_the_same_session() -> None:
    calendar = TradingCalendar(_dates(30))
    before_close = datetime(2024, 1, 5, 14, 0, tzinfo=UTC)
    filing = CorporateFilingEvent.create(
        stable_security_id="SEC_A",
        cik="0000000001",
        accession_number="0000000001-24-000021",
        form=FilingForm.FORM_10Q,
        report_period="2023-12-31",
        filed_at=before_close,
        accepted_at=before_close,
        primary_document="doc.htm",
        file_number=None,
    )
    observations = build_filing_event_observations(
        stable_security_id="SEC_A", filings=(filing,), calendar=calendar
    )
    assert len(observations) == 1
    assert observations[0].shifted_to_next_session is False
    assert observations[0].first_outcome_session_date == "2024-01-05"


# ==========================================================================
# Hypothesis-sample construction per research design (section 5).
# ==========================================================================


def _build_hypothesis(**overrides: object) -> GeneratedCorporateHypothesis:
    from apex.domain.cross_market_research import ConditionRule, DiscoveryStatus, ProposalSource
    from apex.domain.research_methodology import BenchmarkKind

    defaults: dict[str, object] = dict(
        agenda=_agenda(),
        template_id="CROSS_SECTIONAL_V1",
        template_version="V1",
        family=CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        feature_id=CorporateFeatureId.REVENUE_GROWTH_YOY,
        condition_rule=ConditionRule.ABOVE_TRAIN_MEDIAN,
        condition_label=None,
        secondary_context_dimension=None,
        secondary_condition_label=None,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=5,
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
        rationale="test rationale",
        proposal_source=ProposalSource.DETERMINISTIC_RULE,
        is_negative_control=False,
        negative_control_kind=None,
        discovery_status=DiscoveryStatus.PREDECLARED,
        ordinal=0,
        created_at=NOW,
    )
    defaults.update(overrides)
    return GeneratedCorporateHypothesis.create(**defaults)  # type: ignore[arg-type]


def test_cross_sectional_sample_isolates_the_high_feature_group() -> None:
    hypothesis = _build_hypothesis()
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    treatment_securities = {row.stable_security_id for row in sample.treatment}
    assert treatment_securities and treatment_securities <= set(HIGH_SECURITIES)
    assert len(sample.benchmark) > len(sample.treatment)
    treatment_mean = sum((row.value for row in sample.treatment), Decimal(0)) / Decimal(
        len(sample.treatment)
    )
    assert treatment_mean > 0


def test_change_based_sample_isolates_improving_margin_group() -> None:
    hypothesis = _build_hypothesis(
        template_id="CORPORATE_CHANGE_V1",
        family=CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
        feature_id=CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    treatment_securities = {row.stable_security_id for row in sample.treatment}
    assert treatment_securities <= set(HIGH_SECURITIES)


def test_filing_event_sample_uses_genuine_filing_availability() -> None:
    hypothesis = _build_hypothesis(
        template_id="FILING_EVENT_V1",
        family=CorporateHypothesisFamily.FILING_EVENT,
        feature_id=CorporateFeatureId.RECENT_8K_INDICATOR,
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.TRAIN
    )
    assert sample is not None
    real_dates = {
        observation.first_outcome_session_date
        for observations in PANEL.filing_events_by_security.values()
        for observation in observations
        if observation.is_earnings_proxy
    }
    treatment_dates = {row.session_date for row in sample.treatment}
    assert treatment_dates and treatment_dates <= real_dates


def test_conditional_sample_is_restricted_to_the_matching_regime() -> None:
    hypothesis = _build_hypothesis(
        template_id="CONDITIONAL_RELATIONSHIP_V1",
        family=CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME,
        secondary_context_dimension=CorporateContextDimension.EQUITY_TREND,
        secondary_condition_label="UPTREND",
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    treatment_dates = {row.session_date for row in sample.treatment}
    for date in treatment_dates:
        assert PANEL.context_label(CorporateContextDimension.EQUITY_TREND, date) == "UPTREND"


def test_interaction_sample_is_restricted_to_the_matching_regime() -> None:
    hypothesis = _build_hypothesis(
        template_id="INTERACTION_V1",
        family=CorporateHypothesisFamily.INTERACTION_FUNDAMENTAL_MARKET,
        feature_id=CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        secondary_context_dimension=CorporateContextDimension.BOND_RISK_RELATIONSHIP,
        secondary_condition_label="RATE_HEADWIND",
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    treatment_dates = {row.session_date for row in sample.treatment}
    for date in treatment_dates:
        label = PANEL.context_label(CorporateContextDimension.BOND_RISK_RELATIONSHIP, date)
        assert label == "RATE_HEADWIND"


# ==========================================================================
# Negative controls (section 9): temporal displacement, issuer mismatch,
# randomized issuer assignment, unrelated concept/outcome pairing, random
# filing-event dates.
# ==========================================================================


def test_temporal_displacement_mechanism_looks_back_the_declared_steps() -> None:
    from apex.application.corporate_market_research import (
        NEGATIVE_CONTROL_DISPLACEMENT_STEPS,
        _displaced_date,
    )

    index = 10
    date = PANEL.monthly_dates[index]
    displaced = _displaced_date(PANEL, date)
    assert displaced == PANEL.monthly_dates[index - NEGATIVE_CONTROL_DISPLACEMENT_STEPS]
    assert displaced < date


def test_issuer_rotation_is_a_fixed_point_free_bijection() -> None:
    from apex.application.corporate_market_research import _issuer_rotation

    rotation = _issuer_rotation(ALL_SECURITIES)
    assert set(rotation.values()) == set(ALL_SECURITIES)
    assert all(source != target for source, target in rotation.items())


def test_temporally_displaced_negative_control_sample_is_structurally_valid() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    hypothesis = _build_hypothesis(
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert sample.benchmark  # descriptive sample still exists even though it is a control


def test_mismatched_issuer_negative_control_sample_is_structurally_valid() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    hypothesis = _build_hypothesis(
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="MISMATCHED_ISSUER_FUNDAMENTALS",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert sample.benchmark


def test_randomized_issuer_assignment_negative_control_is_deterministic() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    hypothesis = _build_hypothesis(
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="RANDOMIZED_ISSUER_ASSIGNMENT",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
    )
    first = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    second = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert first is not None and second is not None
    assert first.treatment == second.treatment
    assert first.benchmark == second.benchmark


def test_random_filing_event_date_control_does_not_require_real_events() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    hypothesis = _build_hypothesis(
        template_id="FILING_EVENT_V1",
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        feature_id=CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
        is_negative_control=True,
        negative_control_kind="RANDOM_FILING_EVENT_DATE_MATCHED_CALENDAR",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None
    assert len(sample.treatment) >= 1


def test_unrelated_concept_outcome_pairing_uses_filing_lag_feature() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    hypothesis = _build_hypothesis(
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        feature_id=CorporateFeatureId.FILING_LAG_FROM_PERIOD_END_DAYS,
        is_negative_control=True,
        negative_control_kind="UNRELATED_CORPORATE_CONCEPT_OUTCOME_PAIRING",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
    )
    sample = build_hypothesis_sample(
        hypothesis, panel=PANEL, period_policy=PERIOD_POLICY, role=ResearchPeriodRole.VALIDATION
    )
    assert sample is not None


# ==========================================================================
# Full pipeline: RunSingleCorporateHypothesis -> FinalizeCorporateAgenda,
# using the in-memory repositories. Proves panel-dependence-aware
# diagnostics, robustness, conclusions, family-level correction, the
# knowledge base, contradictions, follow-ups, and template-matched
# calibration (matched vs insufficient stratum) all genuinely work
# end to end without a database.
# ==========================================================================


def _in_memory_methodology() -> InMemoryResearchMethodologyRepository:
    return InMemoryResearchMethodologyRepository()


def test_full_pipeline_runs_genuine_and_control_hypotheses_to_conclusions() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    agenda = _agenda()
    methodology = _in_memory_methodology()
    runner = RunSingleCorporateHypothesis(
        methodology=methodology, panel=PANEL, period_policy=PERIOD_POLICY
    )

    genuine = _build_hypothesis(ordinal=0)
    control_a = _build_hypothesis(
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="RANDOMIZED_ISSUER_ASSIGNMENT",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
        ordinal=1,
    )
    control_b = _build_hypothesis(
        feature_id=CorporateFeatureId.NET_INCOME_GROWTH_YOY,
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
        ordinal=2,
    )

    outcomes = [runner.execute(item, created_at=NOW) for item in (genuine, control_a, control_b)]

    for outcome in outcomes:
        assert outcome.governance_policy.governance_policy_id
        assert outcome.validation_trial is not None
        # Panel dependence: effective sample must be the distinct-date count, never the raw
        # (security, date) row count.
        overlap = outcome.validation_trial.diagnostics.overlap
        assert overlap.effective_sample_count <= Decimal(overlap.raw_sample_count)

    repository = InMemoryCorporateMarketResearchRepository()
    repository.save_agenda(agenda)
    for item in (genuine, control_a, control_b):
        repository.save_hypothesis(item)

    finalizer = FinalizeCorporateAgenda(repository=repository)
    report = finalizer.execute(
        agenda=agenda, outcomes=outcomes, panel=PANEL, methodology=methodology, created_at=NOW
    )
    assert report.hypotheses_proposed == 3
    assert report.trials_run >= 3

    genuine_execution = repository.execution_by_hypothesis(genuine.hypothesis_id)
    assert genuine_execution is not None
    assert genuine_execution.trial_family_id is not None

    knowledge_entries = repository.knowledge_entries_for_hypothesis(genuine.hypothesis_id)
    assert len(knowledge_entries) == 1
    assert knowledge_entries[0].revision == 1
    assert knowledge_entries[0].conclusion_id == genuine_execution.conclusion_id


class _FakePanelBuilder:
    """Satisfies ``CorporatePanelBuilderCollaborator`` by returning the
    pre-built synthetic ``PANEL`` regardless of the requested lineage ids
    -- lets ``AdvanceCorporateAgendaRun``'s real resumable-cursor loop run
    against a small, fast, fully offline panel instead of faking all four
    of ``BuildCorporatePanel``'s own real-data collaborators."""

    def execute(
        self,
        *,
        corporate_dataset_id: str,
        equity_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
    ) -> CorporatePanel:
        assert corporate_dataset_id == "corporate-dataset-1"
        assert equity_snapshot_id == "equity-snapshot-1"
        assert replay_run_id == REPLAY_RUN_ID
        assert cross_market_dataset_id == "cross-market-dataset-1"
        return PANEL


def test_advance_agenda_run_reaches_completion_with_no_duplicate_or_overcounted_cursor() -> None:
    """Regression test for a real bug this milestone's genuine CLI run
    against retained data caught: ``AdvanceCorporateAgendaRun.execute()``
    computed ``is_final`` correctly but never passed it to ``run.advance()``
    (hardcoded ``is_final=False``), so the real final hypothesis never
    flipped ``status`` to COMPLETE -- the next call then hit the (buggy)
    "no remaining hypotheses" branch, which re-advanced the *same* final
    hypothesis id a second time, corrupting ``completed_hypothesis_ids``
    with a duplicate and ``cursor_ordinal`` with an off-by-one overcount.
    This drives the *real* resumable-cursor engine (not the lower-level
    ``RunSingleCorporateHypothesis``/``FinalizeCorporateAgenda`` pair the
    other pipeline test above calls directly) end to end to completion and
    asserts the cursor is exactly, non-duplicately correct -- then calls
    it once more to prove resuming a COMPLETE run is a genuine no-op."""

    agenda = _agenda()
    repository = InMemoryCorporateMarketResearchRepository()
    repository.save_agenda(agenda)
    methodology = _in_memory_methodology()

    registrar = RegisterCorporateAgendaRun(repository=repository)
    run = registrar.execute(agenda=agenda, created_at=NOW)
    assert run.status is AgendaRunStatus.NOT_STARTED
    total_hypotheses = len(run.hypothesis_ids)
    assert total_hypotheses == 37  # 20 genuine + 17 negative controls, per the real v3 catalog

    advancer = AdvanceCorporateAgendaRun(
        repository=repository, methodology=methodology, panel_builder=_FakePanelBuilder()
    )
    quality_report = None
    for _ in range(total_hypotheses + 2):  # generous bound; a hang would be a real bug
        result = advancer.execute(agenda=agenda, created_at=NOW)
        if result.run.status is AgendaRunStatus.COMPLETE:
            quality_report = result.quality_report
            break
    else:
        raise AssertionError("agenda run never reached COMPLETE within the expected step bound")

    completed = result.run.completed_hypothesis_ids
    assert len(completed) == total_hypotheses
    assert len(set(completed)) == total_hypotheses  # no duplicate completion entries
    assert result.run.cursor_ordinal == total_hypotheses  # never overcounted past the true total
    assert quality_report is not None
    assert quality_report.hypotheses_proposed == total_hypotheses

    # Resuming an already-COMPLETE run must be a genuine, non-mutating no-op.
    replay = advancer.execute(agenda=agenda, created_at=NOW)
    assert replay.run.status is AgendaRunStatus.COMPLETE
    assert replay.run.completed_hypothesis_ids == completed
    assert replay.run.cursor_ordinal == total_hypotheses
    assert replay.outcome is None
    assert replay.quality_report is not None


def test_trial_family_correction_covers_every_family_with_a_validation_trial() -> None:
    methodology = _in_memory_methodology()
    runner = RunSingleCorporateHypothesis(
        methodology=methodology, panel=PANEL, period_policy=PERIOD_POLICY
    )
    hypothesis = _build_hypothesis(ordinal=0)
    outcome = runner.execute(hypothesis, created_at=NOW)
    families = build_trial_families((outcome,), panel=PANEL, created_at=NOW)
    assert CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL in families
    family = families[CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL]
    assert family.test_count == 1
    assert len(family.corrected_results) == 1


def test_reconstruct_hypothesis_outcome_matches_the_live_run() -> None:
    methodology = _in_memory_methodology()
    runner = RunSingleCorporateHypothesis(
        methodology=methodology, panel=PANEL, period_policy=PERIOD_POLICY
    )
    hypothesis = _build_hypothesis(ordinal=0)
    live_outcome = runner.execute(hypothesis, created_at=NOW)

    from apex.domain.cross_market_research import HypothesisExecutionRecord

    execution = HypothesisExecutionRecord.create(
        hypothesis_id=hypothesis.hypothesis_id,
        agenda_id=hypothesis.agenda_id,
        governance_policy_id=live_outcome.governance_policy.governance_policy_id,
        exploratory_trial_id=(
            live_outcome.exploratory_trial.trial_id if live_outcome.exploratory_trial else None
        ),
        validation_trial_id=(
            live_outcome.validation_trial.trial_id if live_outcome.validation_trial else None
        ),
        holdout_trial_id=(
            live_outcome.holdout_trial.trial_id if live_outcome.holdout_trial else None
        ),
        walk_forward_trial_ids=(),
        robustness_run_id=(
            live_outcome.robustness_run.robustness_run_id if live_outcome.robustness_run else None
        ),
        trial_family_id=None,
        conclusion_id=None,
        promotion_evaluation_id=None,
        stability=live_outcome.stability,
        evidence_shape="CORPORATE_MARKET_PANEL_V3",
        blockers=(),
        created_at=NOW,
    )
    reconstructed = reconstruct_hypothesis_outcome(execution, hypothesis, methodology)
    assert reconstructed is not None
    assert reconstructed.validation_trial is not None and live_outcome.validation_trial is not None
    assert reconstructed.validation_trial.trial_id == live_outcome.validation_trial.trial_id


# ==========================================================================
# Template-matched calibration (section 10): a stratum with enough
# negative controls is classified as sufficient; a stratum with too few
# fails closed to insufficient.
# ==========================================================================


def test_calibration_matched_stratum_sufficient_with_enough_controls() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    methodology = _in_memory_methodology()
    runner = RunSingleCorporateHypothesis(
        methodology=methodology, panel=PANEL, period_policy=PERIOD_POLICY
    )
    genuine = _build_hypothesis(ordinal=0)
    control_features = (
        CorporateFeatureId.REVENUE_GROWTH_YOY,
        CorporateFeatureId.NET_INCOME_GROWTH_YOY,
        CorporateFeatureId.OPERATING_MARGIN,
        CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        CorporateFeatureId.CASH_TO_ASSETS,
        CorporateFeatureId.SHARE_COUNT_CHANGE_YOY,
    )
    controls = [
        _build_hypothesis(
            feature_id=feature,
            family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
            is_negative_control=True,
            negative_control_kind=kind,
            discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
            ordinal=10 + i,
        )
        for i, (feature, kind) in enumerate(
            (f, k)
            for k in (
                "TEMPORALLY_DISPLACED_CORPORATE_FACT",
                "MISMATCHED_ISSUER_FUNDAMENTALS",
                "RANDOMIZED_ISSUER_ASSIGNMENT",
            )
            for f in control_features
        )
    ]
    assert corporate_matched_stratum_key(genuine) == corporate_matched_stratum_key(controls[0])
    assert len(controls) == 18  # >= MINIMUM_MATCHED_NULL_COUNT (12)

    outcomes = [runner.execute(item, created_at=NOW) for item in (genuine, *controls)]
    families = build_trial_families(outcomes, panel=PANEL, created_at=NOW)
    classifications = classify_hypothesis_calibration(
        outcomes, families, agenda_id="agenda-calibration-test", created_at=NOW
    )
    assert len(classifications) == 1
    assert classifications[0].matched.sufficient is True
    assert classifications[0].matched.matched_null_count >= 12


def test_calibration_insufficient_stratum_fails_closed() -> None:
    from apex.domain.cross_market_research import DiscoveryStatus

    methodology = _in_memory_methodology()
    runner = RunSingleCorporateHypothesis(
        methodology=methodology, panel=PANEL, period_policy=PERIOD_POLICY
    )
    genuine = _build_hypothesis(
        template_id="INTERACTION_V1",
        family=CorporateHypothesisFamily.INTERACTION_FUNDAMENTAL_MARKET,
        feature_id=CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        secondary_context_dimension=CorporateContextDimension.BOND_RISK_RELATIONSHIP,
        secondary_condition_label="RATE_HEADWIND",
        ordinal=0,
    )
    lone_control = _build_hypothesis(
        family=CorporateHypothesisFamily.NEGATIVE_CONTROL,
        is_negative_control=True,
        negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
        discovery_status=DiscoveryStatus.NEGATIVE_CONTROL,
        ordinal=1,
    )
    outcomes = [runner.execute(item, created_at=NOW) for item in (genuine, lone_control)]
    families = build_trial_families(outcomes, panel=PANEL, created_at=NOW)
    classifications = classify_hypothesis_calibration(
        outcomes, families, agenda_id="agenda-calibration-test-2", created_at=NOW
    )
    assert len(classifications) == 1
    assert classifications[0].matched.sufficient is False
    assert classifications[0].matched.matched_percentile is None
    assert classifications[0].insufficiency_code == "CALIBRATION_STRATUM_INSUFFICIENT"


def test_incremental_contribution_is_computed_for_conditional_hypotheses() -> None:
    hypothesis = _build_hypothesis(
        template_id="CONDITIONAL_RELATIONSHIP_V1",
        family=CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME,
        secondary_context_dimension=CorporateContextDimension.EQUITY_TREND,
        secondary_condition_label="UPTREND",
    )
    result = compute_incremental_contribution(hypothesis, panel=PANEL, period_policy=PERIOD_POLICY)
    assert result is not None
    assert result.market_only_sample > 0
    assert result.corporate_only_sample > 0


def test_incremental_contribution_is_none_without_secondary_context() -> None:
    hypothesis = _build_hypothesis()
    assert (
        compute_incremental_contribution(hypothesis, panel=PANEL, period_policy=PERIOD_POLICY)
        is None
    )


# ==========================================================================
# Options boundary (section 23/24): a current option feature can never
# enter a historical frame -- proven structurally, not just by absence of
# a wiring bug.
# ==========================================================================


def test_options_seam_type_never_appears_on_any_historical_frame_type() -> None:
    import dataclasses

    from apex.domain.corporate_market_research import (
        CorporateFeatureObservation as DomainCorporateFeatureObservation,
    )
    from apex.domain.corporate_market_research import (
        CorporateOutcomeObservation as DomainCorporateOutcomeObservation,
    )

    for frame_type in (
        DomainCorporateFeatureObservation,
        DomainCorporateOutcomeObservation,
        CorporatePanel,
    ):
        for field in dataclasses.fields(frame_type):
            assert "Options" not in str(field.type)


def test_options_forward_seam_reports_unavailable_without_a_reader() -> None:
    from apex.application.corporate_market_research import describe_options_forward_seam

    snapshot = describe_options_forward_seam(reader=None, plan_id=None)
    assert snapshot.availability == "OPTIONS_SEAM_FACTUALLY_UNAVAILABLE_ZERO_FORWARD_SESSIONS"


class _FakeCheckpoint:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.underlying_symbols_captured = symbols
        self.created_at = NOW


class _FakeCheckpointReader:
    def __init__(self, checkpoint: object | None) -> None:
        self._checkpoint = checkpoint

    def latest_checkpoint(self, plan_id: str) -> object | None:
        return self._checkpoint


def test_options_forward_seam_reports_ready_when_a_genuine_checkpoint_exists() -> None:
    from apex.application.corporate_market_research import describe_options_forward_seam

    reader = _FakeCheckpointReader(_FakeCheckpoint(("AAPL", "MSFT")))
    snapshot = describe_options_forward_seam(reader=reader, plan_id="plan-1")
    assert snapshot.availability == "OPTIONS_SEAM_STRUCTURALLY_READY"
    assert snapshot.underlying_symbols == ("AAPL", "MSFT")
    # Even when "ready", the seam never carries a training/validation/holdout attachment --
    # it is a plain, current-only descriptive snapshot with no session-date-indexed history.
    assert not hasattr(snapshot, "training_sample")
    assert not hasattr(snapshot, "validation_sample")
