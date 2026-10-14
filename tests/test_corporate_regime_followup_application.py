"""Application-level proofs for Corporate Calibration-Qualified Regime
Follow-Up v1: specification freezing against the real, in-memory-generated
v3 hypothesis catalog (including its two genuine CONDITIONAL_MARKET_REGIME
precedent hypotheses), the full spec -> run pipeline against a small,
fully synthetic, hand-built ``CorporatePanel`` (no database, no provider
access), idempotent replay, regime-group adequacy (both the adequate and
sparse-subgroup paths), matched-null calibration, robustness, incremental
contribution, and the confirmation-boundary/CLI-interface guarantees --
mirroring ``tests/test_corporate_calibration_depth_application.py``'s own
offline-fixture convention.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.corporate_calibration_depth import (
    InMemoryCorporateCalibrationDepthRepository,
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
)
from apex.application.corporate_regime_followup import (
    CorporateRegimeFollowUpApplicationError,
    CreateCorporateRegimeFollowUpSpecification,
    InMemoryCorporateRegimeFollowUpRepository,
    RunCorporateRegimeFollowUp,
)
from apex.application.research_methodology import InMemoryResearchMethodologyRepository
from apex.domain.corporate_calibration_depth import (
    CorporateCalibrationPlan,
    CorporateReassessment,
    CorporateReassessmentSubjectKind,
    CorporateStratumTarget,
)
from apex.domain.corporate_fundamentals import CorporateFilingEvent, FilingForm
from apex.domain.corporate_market_research import (
    ALLOWED_HORIZONS_SESSIONS,
    CorporateFeatureId,
    CorporateFeatureObservation,
    CorporateMarketResearchAgenda,
    CorporateOutcomeId,
)
from apex.domain.corporate_regime_followup import (
    EvidenceGrade,
    RegimeFollowUpConclusion,
    RegimeSplitRole,
)
from apex.domain.research_calibration_v2 import (
    ConditioningShape,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    ResearchDesign,
    classify_follow_up_v2,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

NOW = datetime(2026, 8, 10, tzinfo=UTC)
REPLAY_RUN_ID = "regime-followup-replay-run-test"

HIGH_SECURITIES = ("SEC_A", "SEC_B", "SEC_C", "SEC_D", "SEC_E")
LOW_SECURITIES = ("SEC_F", "SEC_G", "SEC_H", "SEC_I", "SEC_J")
ALL_SECURITIES = HIGH_SECURITIES + LOW_SECURITIES
SECTOR_BY_SECURITY = {
    "SEC_A": "TECHNOLOGY",
    "SEC_B": "TECHNOLOGY",
    "SEC_C": "FINANCIALS",
    "SEC_D": "HEALTH_CARE",
    "SEC_E": "INDUSTRIALS",
    "SEC_F": "TECHNOLOGY",
    "SEC_G": "FINANCIALS",
    "SEC_H": "HEALTH_CARE",
    "SEC_I": "INDUSTRIALS",
    "SEC_J": "MATERIALS",
}
DAILY_RETURN_BY_SECURITY = {
    **{s: Decimal("0.0015") for s in HIGH_SECURITIES},
    **{s: Decimal("-0.0005") for s in LOW_SECURITIES},
}


def _agenda() -> CorporateMarketResearchAgenda:
    return CorporateMarketResearchAgenda.create(
        version="V3",
        objective="Regime follow-up application-test objective.",
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
    values = []
    price = Decimal(100)
    generator = random.Random(jitter_seed)
    for _ in range(count):
        values.append(price)
        jitter = Decimal(str(round(generator.uniform(-0.0015, 0.0015), 6)))
        price = price * (Decimal(1) + daily_return + jitter)
    return tuple(values)


def _sample_filings(security: str) -> tuple[CorporateFilingEvent, ...]:
    accepted = datetime(2024, 3, 4, 14, 0, tzinfo=UTC)
    index = ALL_SECURITIES.index(security)
    cik = f"{index + 1:010d}"
    return (
        CorporateFilingEvent.create(
            stable_security_id=security,
            cik=cik,
            accession_number=f"{cik}-24-{1:06d}",
            form=FilingForm.FORM_10Q,
            report_period="2023-12-31",
            filed_at=accepted,
            accepted_at=accepted,
            primary_document="doc1.htm",
            file_number=None,
        ),
    )


def build_synthetic_panel(
    *,
    calendar_length: int = 400,
    monthly_stride: int = 5,
    uptrend_fraction: Decimal = Decimal("0.5"),
) -> CorporatePanel:
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
        growth = Decimal("0.25") if is_high else Decimal("-0.05")
        observations = []
        for as_of_date in monthly_dates:
            values = (
                (CorporateFeatureId.REVENUE_GROWTH_YOY, growth),
                (CorporateFeatureId.NET_INCOME_GROWTH_YOY, growth),
                (CorporateFeatureId.OPERATING_CASH_FLOW_GROWTH_YOY, growth),
                (CorporateFeatureId.OPERATING_MARGIN, Decimal("0.15")),
                (CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY, growth / 10),
                (CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS, Decimal("0.30")),
                (CorporateFeatureId.CASH_TO_ASSETS, Decimal("0.20")),
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

    uptrend_count = int(len(monthly_dates) * uptrend_fraction)
    context_labels: dict[str, dict[str, str]] = {
        CorporateContextDimension.EQUITY_TREND: {},
        CorporateContextDimension.EQUITY_VOLATILITY: {},
        CorporateContextDimension.EQUITY_RISK_TONE: {},
        CorporateContextDimension.BOND_RISK_RELATIONSHIP: {},
    }
    for index, date_str in enumerate(monthly_dates):
        context_labels[CorporateContextDimension.EQUITY_TREND][date_str] = (
            "UPTREND" if index < uptrend_count else "RANGE_BOUND"
        )
        context_labels[CorporateContextDimension.EQUITY_VOLATILITY][date_str] = (
            "ELEVATED_VOLATILITY" if index % 3 == 0 else "NORMAL_VOLATILITY"
        )
        context_labels[CorporateContextDimension.EQUITY_RISK_TONE][date_str] = "NEUTRAL"
        context_labels[CorporateContextDimension.BOND_RISK_RELATIONSHIP][date_str] = "DECOUPLED"

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


ADEQUATE_PANEL = build_synthetic_panel()
# Identical calendar/date axis to ADEQUATE_PANEL (same length/stride, so
# ``dates_all`` still lines up with a specification frozen against
# ADEQUATE_PANEL) -- only the EQUITY_TREND label distribution is skewed so
# heavily toward UPTREND that REGIME_NOT_ADVANCING drops below the
# regime-group adequacy floor. Used to test the run-time adequacy gate in
# isolation via a swapped panel builder, not by relying on the v3 agenda's
# own (fragile, data-dependent) follow-up-proposal machinery to fire again
# for a second, differently-shaped fixture.
SPARSE_PANEL = build_synthetic_panel(uptrend_fraction=Decimal("0.97"))


class _FakePanelBuilder:
    def __init__(self, panel: CorporatePanel) -> None:
        self._panel = panel

    def execute(self, **_kwargs: object) -> CorporatePanel:
        return self._panel


def _run_v3_agenda_in_memory(
    panel: CorporatePanel,
) -> tuple[
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
        repository=repository, methodology=methodology, panel_builder=_FakePanelBuilder(panel)
    )
    for _step in range(60):
        result = advancer.execute(agenda=agenda, created_at=NOW)
        if result.run.status.value == "COMPLETE":
            break
    else:
        raise AssertionError("v3 agenda fixture did not reach COMPLETE")
    return repository, methodology, agenda


def _parent_hypothesis(repository: InMemoryCorporateMarketResearchRepository, agenda_id: str):
    for item in repository.hypotheses_for_agenda(agenda_id):
        if (
            not item.is_negative_control
            and item.feature_id is CorporateFeatureId.NET_INCOME_GROWTH_YOY
            and item.outcome_id is CorporateOutcomeId.SUBSEQUENT_RETURN
            and item.template_id == "CROSS_SECTIONAL_V1"
        ):
            return item
    raise AssertionError("v3 fixture did not generate the expected parent hypothesis")


def _split_by_regime_follow_up(
    repository: InMemoryCorporateMarketResearchRepository, hypothesis_id: str
):
    for item in repository.list_follow_ups():
        if item.parent_hypothesis_id == hypothesis_id and item.kind.value == "SPLIT_BY_REGIME":
            return item
    raise AssertionError(
        "v3 fixture did not propose a SPLIT_BY_REGIME follow-up for the parent hypothesis"
    )


def _seed_calibration_qualification(
    calibration_repository: InMemoryCorporateCalibrationDepthRepository,
    market_repository: InMemoryCorporateMarketResearchRepository,
    agenda: CorporateMarketResearchAgenda,
    hypothesis,
    conclusion_id: str,
    follow_up_id: str,
) -> None:
    target = CorporateStratumTarget.create(
        matched_stratum="CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|NUMERIC_SPLIT",
        research_design=ResearchDesign.CROSS_SECTIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        outcome_id=CorporateOutcomeId.SUBSEQUENT_RETURN,
        horizon_sessions=20,
        genuine_hypothesis_ids=(hypothesis.hypothesis_id,),
        backing_feature_ids=(CorporateFeatureId.NET_INCOME_GROWTH_YOY,),
        original_matched_null_count=16,
        target_new_control_count=24,
    )
    plan = CorporateCalibrationPlan.create(
        version="TEST",
        objective="Regime follow-up application-test calibration plan.",
        source_agenda_id=agenda.agenda_id,
        corporate_dataset_id=agenda.corporate_dataset_id,
        equity_snapshot_id=agenda.equity_snapshot_id,
        replay_run_id=agenda.replay_run_id,
        cross_market_dataset_id=agenda.cross_market_dataset_id,
        stratum_targets=(target,),
        controls_per_stratum=24,
        random_seed_policy="FIXED_TEST_SEED",
        registered_at=NOW,
    )
    calibration_repository.save_plan(plan)
    matched = MatchedCalibrationResult(
        matched_stratum=target.matched_stratum,
        matched_null_count=40,
        sufficient=True,
        matched_percentile=Decimal("0.95"),
        pooled_null_count=233,
        pooled_percentile=Decimal("0.95"),
    )
    reassessment = CorporateReassessment.create(
        calibration_id=plan.calibration_id,
        subject_id=hypothesis.hypothesis_id,
        subject_kind=CorporateReassessmentSubjectKind.HYPOTHESIS,
        matched_stratum=target.matched_stratum,
        original_conclusion_id=conclusion_id,
        original_conclusion_state="WEAK_SUPPORT",
        original_evidence_grade="EXPLORATORY",
        original_matched_null_count=16,
        new_matched_null_count=40,
        new_matched_percentile=Decimal("0.95"),
        new_pooled_null_count=233,
        new_evidence_grade=EvidenceGrade.SUGGESTIVE,
        cohens_d=Decimal("0.076"),
        rationale="Application-test SUGGESTIVE reassessment.",
        created_at=NOW,
    )
    calibration_repository.save_reassessment(reassessment)
    qualification = classify_follow_up_v2(matched=matched, evidence_grade=EvidenceGrade.SUGGESTIVE)
    assert qualification is FollowUpQualificationV2.CALIBRATION_QUALIFIED
    from apex.domain.research_calibration_v2 import FollowUpQualification

    calibration_repository.save_follow_up_qualification(
        FollowUpQualification.create(
            calibration_id=plan.calibration_id,
            follow_up_id=follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            research_design=ResearchDesign.CROSS_SECTIONAL,
            horizon_sessions=20,
            matched=matched,
            evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
            qualification=FollowUpQualificationV2.CALIBRATION_QUALIFIED,
            rationale="Application-test CALIBRATION_QUALIFIED follow-up.",
            created_at=NOW,
        )
    )


def _build_fixture(panel: CorporatePanel):
    market_repository, methodology, agenda = _run_v3_agenda_in_memory(panel)
    hypothesis = _parent_hypothesis(market_repository, agenda.agenda_id)
    follow_up = _split_by_regime_follow_up(market_repository, hypothesis.hypothesis_id)
    execution = market_repository.execution_by_hypothesis(hypothesis.hypothesis_id)
    assert execution is not None
    calibration_repository = InMemoryCorporateCalibrationDepthRepository()
    _seed_calibration_qualification(
        calibration_repository,
        market_repository,
        agenda,
        hypothesis,
        follow_up.parent_conclusion_id,
        follow_up.follow_up_id,
    )
    followup_repository = InMemoryCorporateRegimeFollowUpRepository()
    panel_builder = _FakePanelBuilder(panel)
    return (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    )


# ==========================================================================
# Freeze
# ==========================================================================


def test_specification_freezes_parent_semantics_from_the_real_v3_catalog() -> None:
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    specification = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    ).execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)

    assert specification.feature_id is CorporateFeatureId.NET_INCOME_GROWTH_YOY
    assert specification.outcome_id is CorporateOutcomeId.SUBSEQUENT_RETURN
    assert specification.horizon_sessions == 20
    assert specification.original_calibrated_grade is EvidenceGrade.SUGGESTIVE
    assert specification.matched_null_count == 40
    assert specification.follow_up_qualification == "CALIBRATION_QUALIFIED"
    assert {item.dimension.value for item in specification.regime_selections} == {
        "EQUITY_TREND",
        "EQUITY_VOLATILITY",
    }
    # Section 3's precedent-based derivation: both precedents must be real,
    # retained CONDITIONAL_MARKET_REGIME hypotheses from the same v3 run,
    # never invented ids.
    precedent_ids = {item.precedent_hypothesis_id for item in specification.regime_selections}
    catalog_ids = {
        item.hypothesis_id
        for item in market_repository.hypotheses_for_agenda(
            follow_up.follow_up_id.rsplit("_", 1)[0]
        )
        or ()
    } | {h.hypothesis_id for h in market_repository.hypotheses_for_agenda(specification.agenda_id)}
    assert precedent_ids <= catalog_ids


def test_specification_creation_is_idempotent() -> None:
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    service = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    )
    first = service.execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)
    second = service.execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)
    assert first == second
    assert first.specification_id == second.specification_id


def test_specification_creation_rejects_a_follow_up_that_is_not_calibration_qualified() -> None:
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    # Overwrite the seeded qualification with a non-qualifying one.
    from apex.domain.research_calibration_v2 import FollowUpQualification

    calibration_repository._follow_up_qualifications.clear()  # type: ignore[attr-defined]
    plan = calibration_repository.list_plans()[-1]
    matched = MatchedCalibrationResult(
        matched_stratum="s",
        matched_null_count=10,
        sufficient=False,
        matched_percentile=None,
        pooled_null_count=10,
        pooled_percentile=None,
    )
    calibration_repository.save_follow_up_qualification(
        FollowUpQualification.create(
            calibration_id=plan.calibration_id,
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=follow_up.parent_hypothesis_id,
            research_design=ResearchDesign.CROSS_SECTIONAL,
            horizon_sessions=20,
            matched=matched,
            evidence_grade_v2=EvidenceGrade.EXPLORATORY,
            qualification=FollowUpQualificationV2.CALIBRATION_INSUFFICIENT,
            rationale="Deliberately unqualified for this test.",
            created_at=NOW,
        )
    )
    service = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    )
    with pytest.raises(CorporateRegimeFollowUpApplicationError):
        service.execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)


# ==========================================================================
# Regime: adequate split runs end to end; sparse subgroup fails closed.
# ==========================================================================


def test_run_produces_a_genuine_adequate_primary_split() -> None:
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    specification = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    ).execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)

    result = RunCorporateRegimeFollowUp(
        repository=followup_repository, panel_builder=panel_builder
    ).execute(specification=specification, created_at=NOW)

    assert result.primary_split.adequate is True
    assert result.primary_split.group_a_evidence.distinct_effective_dates >= 8
    assert result.primary_split.group_b_evidence.distinct_effective_dates >= 8
    assert result.primary_split.interaction is not None
    assert result.primary_split.interaction.matched_calibration.matched_null_count == 30
    assert result.primary_split.robustness is not None
    assert result.primary_split.robustness.summary.checks_run == 5
    assert result.primary_split.interaction_stability is not None
    assert len(result.primary_split.interaction_stability.subperiods) == 3
    assert result.conclusion in tuple(RegimeFollowUpConclusion)
    assert result.evidence_grade in tuple(EvidenceGrade)
    controls = followup_repository.controls_for_specification(
        specification.specification_id, role=RegimeSplitRole.PRIMARY
    )
    assert len(controls) == 30
    assert len({item.control_id for item in controls}) == 30  # every replicate is distinct


def test_run_is_idempotent_and_never_recomputes() -> None:
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    specification = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    ).execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)

    runner = RunCorporateRegimeFollowUp(repository=followup_repository, panel_builder=panel_builder)
    first = runner.execute(specification=specification, created_at=NOW)
    controls_after_first = followup_repository.controls_for_specification(
        specification.specification_id
    )
    second = runner.execute(specification=specification, created_at=NOW)
    assert first == second
    controls = followup_repository.controls_for_specification(specification.specification_id)
    assert len(controls) == len(controls_after_first)  # not duplicated by the second call
    # Both regime splits are adequate against this fixture, so both consume
    # their own 30-control budget (never more, never re-run).
    assert len(controls) == 60
    assert (
        len(
            followup_repository.controls_for_specification(
                specification.specification_id, role=RegimeSplitRole.PRIMARY
            )
        )
        == 30
    )
    assert (
        len(
            followup_repository.controls_for_specification(
                specification.specification_id, role=RegimeSplitRole.SECONDARY
            )
        )
        == 30
    )


def test_run_fails_closed_on_a_sparse_regime_subgroup() -> None:
    # Spec-creation lineage comes from the ADEQUATE_PANEL v3 fixture (the
    # only one guaranteed to have actually proposed a SPLIT_BY_REGIME
    # follow-up); the run itself is then pointed at SPARSE_PANEL -- the
    # identical date axis with EQUITY_TREND skewed so REGIME_NOT_ADVANCING
    # falls below the adequacy floor -- to exercise the fail-closed gate in
    # isolation.
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    specification = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    ).execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)

    sparse_panel_builder = _FakePanelBuilder(SPARSE_PANEL)
    result = RunCorporateRegimeFollowUp(
        repository=followup_repository, panel_builder=sparse_panel_builder
    ).execute(specification=specification, created_at=NOW)

    assert result.primary_split.adequate is False
    assert "BELOW_MINIMUM" in (result.primary_split.inadequacy_reason or "")
    assert result.primary_split.interaction is None
    assert result.primary_split.evidence_grade is None
    assert result.conclusion is RegimeFollowUpConclusion.INSUFFICIENT_EVIDENCE
    # No null controls are wasted computing a PRIMARY interaction the
    # adequacy gate already ruled out (milestone section 10: never expand
    # or spend budget past what a fail-closed gate allows) -- the
    # unrelated SECONDARY split's own EQUITY_VOLATILITY labels were not
    # skewed by this fixture, so it independently remains adequate and is
    # still evaluated (section 3: the secondary split's own fate is never
    # gated by the primary split's outcome).
    assert (
        followup_repository.controls_for_specification(
            specification.specification_id, role=RegimeSplitRole.PRIMARY
        )
        == ()
    )


# ==========================================================================
# Confirmation boundary and interfaces.
# ==========================================================================


def test_result_always_requires_independent_confirmation() -> None:
    (
        market_repository,
        methodology,
        calibration_repository,
        followup_repository,
        panel_builder,
        follow_up,
    ) = _build_fixture(ADEQUATE_PANEL)
    specification = CreateCorporateRegimeFollowUpSpecification(
        repository=followup_repository,
        corporate_market_repository=market_repository,
        methodology=methodology,
        calibration_depth=calibration_repository,
        panel_builder=panel_builder,
    ).execute(follow_up_id=follow_up.follow_up_id, created_at=NOW)
    result = RunCorporateRegimeFollowUp(
        repository=followup_repository, panel_builder=panel_builder
    ).execute(specification=specification, created_at=NOW)
    assert result.independent_confirmation_required is True
    assert len(result.independent_confirmation_reasons) == 3


def test_in_memory_repository_satisfies_the_same_protocol_as_the_postgres_adapter() -> None:
    from apex.adapters.corporate_regime_followup_postgres import (
        PostgresCorporateRegimeFollowUpRepository,
    )

    in_memory_methods = {
        name for name in dir(InMemoryCorporateRegimeFollowUpRepository) if not name.startswith("_")
    }
    postgres_methods = {
        name for name in dir(PostgresCorporateRegimeFollowUpRepository) if not name.startswith("_")
    }
    assert in_memory_methods == postgres_methods
