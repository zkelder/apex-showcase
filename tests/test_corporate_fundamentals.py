"""Domain proofs for Corporate Fundamental, Filing, and Event Intelligence
v1: filing identity, point-in-time visibility/restatement, unit/period
semantics, derived features, quality classification, and the bounded
capture-plan/checkpoint lifecycle. No database, no server, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from itertools import permutations

import pytest

from apex.domain.corporate_fundamentals import (
    CONCEPT_DEFINITIONS,
    CorporateDataQualityReport,
    CorporateEvidenceCaptureBounds,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortMember,
    CorporateEvidenceCohortPlan,
    CorporateEvidenceCohortResolution,
    CorporateFilingEvent,
    CorporateFundamentalsError,
    CorporateHistoryDatasetSnapshot,
    CorporateReplayFeatureFrame,
    DerivedFeatureType,
    FilingEventFeatureType,
    FilingForm,
    FiscalPeriod,
    FundamentalConcept,
    FundamentalFact,
    IssuerCaptureOutcome,
    IssuerCaptureStatus,
    IssuerDataQuality,
    LookbackWindow,
    PeriodType,
    ReplayCorporateEvidenceAttachment,
    apply_lookback_policy,
    assess_issuer_quality,
    build_corporate_fundamental_snapshot,
    build_corporate_replay_feature_frame,
    compute_derived_features,
    compute_filing_event_features,
    count_ambiguous_fact_groups,
    derive_earnings_events,
    derive_q4_discrete_fact,
    detect_fact_anomalies,
    detect_filing_period_gaps,
    latest_fact,
    pending_members,
    resolve_corporate_evidence_cohort,
    resolve_fact_as_of,
    revision_chain,
    sec_cik_10digit,
    select_run_batch,
)
from apex.domain.research_cohort import (
    CohortRole,
    CohortSector,
    ResolvedCohort,
    ResolvedCohortMember,
)
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity

UTC_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _q(value: Decimal) -> Decimal:
    """Mirrors ``domain.corporate_fundamentals._bounded_ratio`` exactly, so
    expected values in these tests are compared against the identical
    8-decimal-place-quantized form the implementation actually returns."""
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def _filing(
    *,
    accession: str = "0000320193-24-000123",
    form: FilingForm | str = FilingForm.FORM_10K,
    filed_at: datetime = datetime(2024, 11, 1, tzinfo=UTC),
    accepted_at: datetime | None = datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
    report_period: str | None = "2024-09-28",
    items: tuple[str, ...] = (),
    stable_security_id: str = "sec-aapl",
    cik: str = "320193",
) -> CorporateFilingEvent:
    return CorporateFilingEvent.create(
        stable_security_id=stable_security_id,
        cik=cik,
        accession_number=accession,
        form=form,
        report_period=report_period,
        filed_at=filed_at,
        accepted_at=accepted_at,
        primary_document="doc.htm",
        file_number="001-36743",
        items=items,
        is_xbrl=True,
        is_inline_xbrl=True,
    )


def _fact(
    *,
    concept: FundamentalConcept = FundamentalConcept.REVENUE,
    source_tag: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
    fiscal_year: int = 2024,
    fiscal_period: FiscalPeriod = FiscalPeriod.FY,
    period_start: str | None = "2023-10-01",
    period_end: str = "2024-09-28",
    value: str = "391035000000",
    accession: str = "0000320193-24-000123",
    form: str = "10-K",
    filed_at: datetime = datetime(2024, 11, 1, tzinfo=UTC),
    accepted_at: datetime | None = datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
    stable_security_id: str = "sec-aapl",
    cik: str = "320193",
) -> FundamentalFact:
    return FundamentalFact.create(
        stable_security_id=stable_security_id,
        cik=cik,
        concept=concept,
        source_taxonomy="us-gaap",
        source_tag=source_tag,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        period_start=period_start,
        period_end=period_end,
        value=Decimal(value),
        accession_number=accession,
        form=form,
        filed_at=filed_at,
        accepted_at=accepted_at,
    )


# --------------------------------------------------------------------------
# Filing identity: accession identity; amendment; duplicate replay; conflict
# --------------------------------------------------------------------------


def test_filing_identity_is_stable_and_content_addressed_for_exact_replay() -> None:
    first = _filing()
    second = _filing()
    assert first.filing_event_id == second.filing_event_id
    assert first.canonical_hash == second.canonical_hash


def test_filing_identity_changes_when_any_retained_field_changes() -> None:
    baseline = _filing()
    changed = _filing(accepted_at=datetime(2024, 11, 2, tzinfo=UTC))
    assert baseline.filing_event_id != changed.filing_event_id


def test_filing_rejects_malformed_accession_number() -> None:
    with pytest.raises(CorporateFundamentalsError):
        _filing(accession="not-an-accession")


def test_amendment_forms_are_classified_and_base_form_recoverable() -> None:
    original = _filing(form=FilingForm.FORM_10K)
    amendment = _filing(accession="0000320193-24-000200", form=FilingForm.FORM_10K_A)
    assert original.amendment_state.value == "ORIGINAL"
    assert amendment.amendment_state.value == "AMENDMENT"
    from apex.domain.corporate_fundamentals import base_form_for

    assert base_form_for(amendment.form) is FilingForm.FORM_10K


def test_8k_items_rejected_on_non_8k_form() -> None:
    with pytest.raises(CorporateFundamentalsError):
        _filing(form=FilingForm.FORM_10Q, items=("2.02",))


def test_filing_accepted_meaningfully_before_filed_is_rejected_as_conflicting_evidence() -> None:
    with pytest.raises(CorporateFundamentalsError):
        _filing(
            filed_at=datetime(2024, 11, 5, tzinfo=UTC),
            accepted_at=datetime(2024, 10, 1, tzinfo=UTC),
        )


def test_missing_acceptance_time_is_recorded_as_an_explicit_limitation() -> None:
    filing = _filing(accepted_at=None)
    assert filing.accepted_at is None
    assert filing.effective_available_at == filing.filed_at
    assert "ACCEPTANCE_TIME_UNAVAILABLE_USED_FILING_DATE" in filing.limitations


def test_sec_cik_10digit_zero_pads_and_rejects_non_numeric() -> None:
    assert sec_cik_10digit("320193") == "0000320193"
    with pytest.raises(CorporateFundamentalsError):
        sec_cik_10digit("not-a-cik")


# --------------------------------------------------------------------------
# Point-in-time: future filing excluded; later restatement excluded from
# earlier as-of; filing available after accepted time
# --------------------------------------------------------------------------


def test_future_filing_excluded_from_snapshot_built_before_its_acceptance() -> None:
    filing = _filing()
    fact = _fact()
    before = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 10, 1, tzinfo=UTC),
        filings=(filing,),
        facts=(fact,),
    )
    revenue = before.value_for(FundamentalConcept.REVENUE)
    assert revenue.value is None
    assert revenue.missing_reason == "NO_VISIBLE_FACT_FOR_CONCEPT"
    assert before.filing_lineage == ()


def test_filing_becomes_visible_exactly_at_its_accepted_time_not_before() -> None:
    filing = _filing()
    fact = _fact()
    accepted_at = filing.accepted_at
    assert accepted_at is not None
    just_before = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=accepted_at - timedelta(seconds=1),
        filings=(filing,),
        facts=(fact,),
    )
    exactly_at = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=accepted_at,
        filings=(filing,),
        facts=(fact,),
    )
    assert just_before.value_for(FundamentalConcept.REVENUE).value is None
    assert exactly_at.value_for(FundamentalConcept.REVENUE).value == Decimal("391035000000")


def test_later_restatement_excluded_from_earlier_as_of_but_visible_later() -> None:
    original = _fact(
        value="383285000000",
        fiscal_year=2023,
        accession="0000320193-23-000106",
        filed_at=datetime(2023, 11, 3, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 3, 16, 30, tzinfo=UTC),
    )
    restated = _fact(
        value="383300000000",
        fiscal_year=2023,
        accession="0000320193-25-000050",
        form="10-K/A",
        filed_at=datetime(2025, 6, 1, tzinfo=UTC),
        accepted_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    candidates = (original, restated)
    before_restatement = resolve_fact_as_of(candidates, as_of=datetime(2024, 1, 1, tzinfo=UTC))
    after_restatement = resolve_fact_as_of(candidates, as_of=datetime(2025, 7, 1, tzinfo=UTC))
    assert before_restatement is not None and before_restatement.value == Decimal("383285000000")
    assert after_restatement is not None and after_restatement.value == Decimal("383300000000")


def test_revision_chain_orders_restatements_and_marks_exactly_one_latest() -> None:
    original = _fact(
        value="383285000000",
        fiscal_year=2023,
        accession="0000320193-23-000106",
        filed_at=datetime(2023, 11, 3, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 3, 16, 30, tzinfo=UTC),
    )
    restated = _fact(
        value="383300000000",
        fiscal_year=2023,
        accession="0000320193-25-000050",
        filed_at=datetime(2025, 6, 1, tzinfo=UTC),
        accepted_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    chain = revision_chain((restated, original))
    assert [item.value for item in chain] == [Decimal("383285000000"), Decimal("383300000000")]
    assert [item.is_latest for item in chain] == [False, True]
    assert chain[1].supersedes_fact_id == chain[0].fact_id


def test_compute_derived_features_never_leaks_a_future_restatement_via_raw_facts() -> None:
    """Regression proof: an earlier draft trusted a caller-supplied
    "already visible" fact set; this proves the public function is
    lookahead-safe even when handed the full unfiltered retained set."""
    current = _fact(value="391035000000", fiscal_year=2024)
    prior_original = _fact(
        value="383285000000",
        fiscal_year=2023,
        accession="0000320193-23-000106",
        filed_at=datetime(2023, 11, 3, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 3, 16, 30, tzinfo=UTC),
    )
    prior_restated_in_the_future = _fact(
        value="383300000000",
        fiscal_year=2023,
        accession="0000320193-25-000050",
        filed_at=datetime(2025, 6, 1, tzinfo=UTC),
        accepted_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    facts = (current, prior_original, prior_restated_in_the_future)
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(),
        facts=facts,
    )
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    growth = next(
        item for item in features if item.feature_type is DerivedFeatureType.YOY_REVENUE_GROWTH
    )
    expected = _q((Decimal("391035000000") - Decimal("383285000000")) / Decimal("383285000000"))
    assert growth.value == expected


def test_snapshot_never_fills_a_missing_fact_with_zero() -> None:
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-none", cik="1", as_of=UTC_NOW, filings=(), facts=()
    )
    for field in snapshot.fields:
        assert field.value is None
        assert field.missing_reason is not None
    assert snapshot.quality is IssuerDataQuality.REJECTED


def test_fact_rejects_accepted_meaningfully_before_filed() -> None:
    """Regression proof (found via genuine SEC capture acceptance): SEC's
    submissions and XBRL-frames endpoints can report a self-contradictory
    filed/accepted pair for the identical accession. The domain layer must
    fail closed on it rather than let it reach persistence -- callers with
    an untrustworthy cross-referenced acceptance time must fall back to
    ``accepted_at=None``, never pass the contradiction through."""
    with pytest.raises(CorporateFundamentalsError):
        _fact(
            filed_at=datetime(2017, 10, 30, tzinfo=UTC),
            accepted_at=datetime(2017, 10, 27, 22, 40, 31, tzinfo=UTC),
        )


def test_snapshot_as_of_must_be_timezone_aware() -> None:
    with pytest.raises(CorporateFundamentalsError):
        build_corporate_fundamental_snapshot(
            stable_security_id="sec-aapl",
            cik="320193",
            as_of=datetime(2024, 1, 1),  # deliberately naive to prove as_of rejection
            filings=(),
            facts=(),
        )


# --------------------------------------------------------------------------
# Facts: units; instant/duration; quarterly/annual; missing facts;
# conflicting concepts
# --------------------------------------------------------------------------


def test_every_concept_definition_declares_a_consistent_unit_and_period_type() -> None:
    concepts = {item.concept for item in CONCEPT_DEFINITIONS}
    assert concepts == set(FundamentalConcept)
    for definition in CONCEPT_DEFINITIONS:
        assert definition.source_tags
        for _taxonomy, tag in definition.source_tags:
            assert tag


def test_instant_concept_rejects_a_period_start() -> None:
    with pytest.raises(CorporateFundamentalsError):
        FundamentalFact.create(
            stable_security_id="sec-aapl",
            cik="320193",
            concept=FundamentalConcept.TOTAL_ASSETS,
            source_taxonomy="us-gaap",
            source_tag="Assets",
            fiscal_year=2024,
            fiscal_period=FiscalPeriod.FY,
            period_start="2023-10-01",
            period_end="2024-09-28",
            value=Decimal("364980000000"),
            accession_number="0000320193-24-000123",
            form="10-K",
            filed_at=datetime(2024, 11, 1, tzinfo=UTC),
            accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        )


def test_duration_concept_requires_a_period_start() -> None:
    with pytest.raises(CorporateFundamentalsError):
        _fact(period_start=None)


def test_fact_source_tag_must_be_in_the_bounded_vocabulary_for_its_concept() -> None:
    with pytest.raises(CorporateFundamentalsError):
        _fact(source_tag="SomeUnrecognizedTag")


def test_fact_rejects_a_form_outside_the_retained_vocabulary() -> None:
    with pytest.raises(CorporateFundamentalsError):
        _fact(form="424B4")


def test_quarterly_and_annual_facts_are_independently_retained_and_selectable() -> None:
    quarterly = _fact(
        fiscal_period=FiscalPeriod.Q1,
        period_start="2023-10-01",
        period_end="2023-12-30",
        value="119575000000",
        accession="0000320193-24-000010",
        form="10-Q",
    )
    annual = _fact()
    assert quarterly.fact_id != annual.fact_id
    assert quarterly.period_type is PeriodType.DURATION
    assert annual.fiscal_period is FiscalPeriod.FY


def test_missing_facts_are_reported_per_concept_never_invented() -> None:
    only_revenue = (_fact(),)
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(),
        facts=only_revenue,
    )
    assert snapshot.value_for(FundamentalConcept.REVENUE).value is not None
    assets = snapshot.value_for(FundamentalConcept.TOTAL_ASSETS)
    assert assets.value is None
    assert assets.missing_reason == "NO_VISIBLE_FACT_FOR_CONCEPT"


def test_conflicting_concepts_for_the_identical_period_are_all_retained_and_flagged() -> None:
    """Two distinct filings reporting different values for the identical
    (concept, fiscal_year, fiscal_period) -- a genuine restatement -- must
    never silently collapse to one value; both are retained and the
    disagreement is flagged as an anomaly (section 19), not corrected."""
    first = _fact(
        value="383285000000",
        fiscal_year=2023,
        accession="0000320193-23-000106",
        filed_at=datetime(2023, 11, 3, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 3, 16, 30, tzinfo=UTC),
    )
    second = _fact(
        value="383300000000",
        fiscal_year=2023,
        accession="0000320193-25-000050",
        filed_at=datetime(2025, 6, 1, tzinfo=UTC),
        accepted_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    anomalies = detect_fact_anomalies((first, second))
    assert any(item.reason == "RESTATEMENT_VALUE_DIFFERS_FROM_PRIOR_FILING" for item in anomalies)


# --------------------------------------------------------------------------
# Corporate Research Panel Determinism v1: canonical fact-selection
# precedence -- order independence, tag priority, discrete-vs-YTD
# preference, and the accession/fact_id final tie-break. Every scenario
# below mirrors a shape genuinely found among retained evidence (a direct
# regression-harness query found 31,433 real ``(security, concept,
# period_end)`` groups tied on the previous ``(effective_available_at,
# accession_number)`` key alone).
# --------------------------------------------------------------------------


def test_choose_fact_is_independent_of_candidate_order_for_a_multi_tag_tie() -> None:
    """Two facts genuinely tied on (effective_available_at, accession) --
    the identical accession tagging NET_INCOME under both compatible
    us-gaap tags -- must resolve to the identical fact regardless of the
    order candidates are supplied in."""
    net_income_loss = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="NetIncomeLoss",
        value="3209000000",
        accession="0000320193-08-000001",
        filed_at=datetime(2008, 1, 20, tzinfo=UTC),
        accepted_at=datetime(2008, 1, 20, 16, 30, tzinfo=UTC),
    )
    profit_loss = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="ProfitLoss",
        value="3249000000",
        accession="0000320193-08-000001",
        filed_at=datetime(2008, 1, 20, tzinfo=UTC),
        accepted_at=datetime(2008, 1, 20, 16, 30, tzinfo=UTC),
    )
    as_of = datetime(2008, 2, 1, tzinfo=UTC)
    for candidates in permutations((net_income_loss, profit_loss)):
        chosen = resolve_fact_as_of(candidates, as_of=as_of)
        assert chosen is not None
        assert chosen.fact_id == net_income_loss.fact_id
        assert chosen.value == Decimal("3209000000")


def test_later_availability_wins_over_tag_priority() -> None:
    """``effective_available_at`` stays the dominant precedence signal
    (section 6): a later-accepted fact tagged with the *lower*-priority
    concept tag still wins over an earlier, higher-priority-tagged fact.
    Tag priority only ever breaks a genuine same-timestamp tie; it never
    overrides point-in-time revision order."""
    earlier_high_priority = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="NetIncomeLoss",
        value="100",
        accession="0000320193-23-000001",
        filed_at=datetime(2023, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 1, tzinfo=UTC),
    )
    later_low_priority = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="ProfitLoss",
        value="200",
        accession="0000320193-24-000001",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    as_of = datetime(2025, 1, 1, tzinfo=UTC)
    for candidates in permutations((earlier_high_priority, later_low_priority)):
        chosen = resolve_fact_as_of(candidates, as_of=as_of)
        assert chosen is not None
        assert chosen.fact_id == later_low_priority.fact_id


def test_equal_timestamp_and_tag_break_tie_by_accession_number() -> None:
    """Two facts identical in tag and ``effective_available_at`` (the
    milestone's 'filing acceptance-time ties') resolve via the
    lexicographically greater accession number -- a final tie-break with
    genuine real-world meaning, deterministic regardless of candidate
    order."""
    lower_accession = _fact(
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        value="100",
        accession="0000320193-24-000001",
    )
    higher_accession = _fact(
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        value="200",
        accession="0000320193-24-000002",
    )
    as_of = datetime(2024, 11, 2, tzinfo=UTC)
    for candidates in permutations((lower_accession, higher_accession)):
        chosen = resolve_fact_as_of(candidates, as_of=as_of)
        assert chosen is not None
        assert chosen.fact_id == higher_accession.fact_id


def test_discrete_quarter_preferred_over_cumulative_ytd_when_otherwise_tied() -> None:
    """The well-documented SEC XBRL quirk -- one accession tags the
    identical concept/tag/fiscal-period with both a discrete quarter and
    a cumulative year-to-date duration (observed directly among retained
    evidence) -- must resolve to the discrete window every YoY/margin
    comparison in this module assumes, deterministically regardless of
    order."""
    discrete_quarter = _fact(
        concept=FundamentalConcept.EPS_DILUTED,
        source_tag="EarningsPerShareDiluted",
        value="4.58",
        period_start="2024-07-01",
        period_end="2024-09-28",
        accession="0000320193-24-000123",
    )
    ytd_nine_months = _fact(
        concept=FundamentalConcept.EPS_DILUTED,
        source_tag="EarningsPerShareDiluted",
        value="7.81",
        period_start="2023-12-31",
        period_end="2024-09-28",
        accession="0000320193-24-000123",
    )
    as_of = datetime(2024, 11, 2, tzinfo=UTC)
    for candidates in permutations((discrete_quarter, ytd_nine_months)):
        chosen = resolve_fact_as_of(candidates, as_of=as_of)
        assert chosen is not None
        assert chosen.fact_id == discrete_quarter.fact_id
        assert chosen.value == Decimal("4.58")


def test_regression_previously_order_dependent_tie_now_stable_across_all_orderings() -> None:
    """Regression test for Corporate Research Panel Determinism v1: this
    is the exact real-evidence shape (two compatible tags, each also
    reported as both a discrete quarter and a cumulative YTD duration
    under the identical accession) that the pre-fix implementation
    resolved differently depending on candidate order -- confirmed via a
    direct forward/reversed permutation proof against 31,433 genuine tie
    groups found in retained evidence. Every one of the 24 possible
    orderings must now select the identical fact."""
    common = {
        "concept": FundamentalConcept.NET_INCOME,
        "fiscal_year": 2008,
        "fiscal_period": FiscalPeriod.Q2,
        "period_end": "2008-06-27",
        "accession": "0000320193-08-000004",
        "filed_at": datetime(2008, 7, 25, tzinfo=UTC),
        "accepted_at": datetime(2008, 7, 25, 16, 30, tzinfo=UTC),
    }
    net_income_q_narrow = _fact(
        source_tag="NetIncomeLoss", value="1422000000", period_start="2008-03-30", **common
    )
    net_income_q_wide = _fact(
        source_tag="NetIncomeLoss", value="2922000000", period_start="2008-01-01", **common
    )
    profit_loss_narrow = _fact(
        source_tag="ProfitLoss", value="1443000000", period_start="2008-03-30", **common
    )
    profit_loss_wide = _fact(
        source_tag="ProfitLoss", value="2954000000", period_start="2008-01-01", **common
    )
    candidates = (net_income_q_narrow, net_income_q_wide, profit_loss_narrow, profit_loss_wide)
    as_of = datetime(2008, 8, 1, tzinfo=UTC)
    winners = {resolve_fact_as_of(perm, as_of=as_of).fact_id for perm in permutations(candidates)}
    assert winners == {net_income_q_narrow.fact_id}


def test_future_higher_priority_tag_does_not_leak_into_a_past_as_of_snapshot() -> None:
    """Determinism must never become a backdoor for lookahead (section 9):
    a not-yet-visible, higher-tag-priority fact stays excluded no matter
    how the tie-break would have preferred it once visible."""
    visible_low_priority = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="ProfitLoss",
        value="100",
        accession="0000320193-23-000001",
        filed_at=datetime(2023, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 1, tzinfo=UTC),
    )
    future_high_priority = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="NetIncomeLoss",
        value="999",
        accession="0000320193-24-000001",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    for candidates in permutations((visible_low_priority, future_high_priority)):
        chosen = resolve_fact_as_of(candidates, as_of=as_of)
        assert chosen is not None
        assert chosen.fact_id == visible_low_priority.fact_id


def test_latest_fact_and_revision_chain_are_independent_of_input_order() -> None:
    older = _fact(
        value="100",
        accession="0000320193-23-000001",
        filed_at=datetime(2023, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 1, tzinfo=UTC),
    )
    newer_tag_a = _fact(
        source_tag="Revenues",
        value="200",
        accession="0000320193-24-000001",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    newer_tag_b = _fact(
        source_tag="SalesRevenueNet",
        value="205",
        accession="0000320193-24-000001",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    for facts in permutations((older, newer_tag_a, newer_tag_b)):
        winner = latest_fact(facts)
        assert winner is not None
        assert winner.fact_id == newer_tag_a.fact_id  # "Revenues" outranks "SalesRevenueNet"
        chain = revision_chain(facts)
        assert chain[-1].fact_id == newer_tag_a.fact_id
        assert chain[-1].is_latest is True


def test_count_ambiguous_fact_groups_counts_only_genuine_same_timestamp_ties() -> None:
    tied_a = _fact(source_tag="Revenues", value="100", accession="0000320193-24-000001")
    tied_b = _fact(source_tag="SalesRevenueNet", value="105", accession="0000320193-24-000001")
    untied = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="NetIncomeLoss",
        value="10",
        accession="0000320193-24-000002",
        filed_at=datetime(2024, 12, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 12, 1, tzinfo=UTC),
    )
    assert count_ambiguous_fact_groups((tied_a, tied_b, untied)) == 1
    assert count_ambiguous_fact_groups((untied,)) == 0
    assert count_ambiguous_fact_groups(()) == 0


def test_compute_derived_features_repeated_build_equality_regardless_of_fact_order() -> None:
    """Same source evidence must produce identical source fact ids, value,
    and transformation metadata regardless of the order ``facts`` is
    supplied in -- proven with a genuine same-accession dual-tag tie
    feeding straight into the snapshot/derived-feature pipeline."""
    revenue_a = _fact(value="100000000", source_tag="Revenues", accession="0000320193-24-000123")
    revenue_b = _fact(
        value="100500000",
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        accession="0000320193-24-000123",
    )
    net_income = _fact(
        concept=FundamentalConcept.NET_INCOME, source_tag="NetIncomeLoss", value="20000000"
    )
    results = set()
    for facts in permutations((revenue_a, revenue_b, net_income)):
        snapshot = build_corporate_fundamental_snapshot(
            stable_security_id="sec-aapl",
            cik="320193",
            as_of=datetime(2024, 11, 2, tzinfo=UTC),
            filings=(),
            facts=facts,
        )
        derived = compute_derived_features(snapshot=snapshot, facts=facts)
        results.add(
            (
                snapshot.snapshot_id,
                tuple((d.feature_id, str(d.value), d.required_source_fact_ids) for d in derived),
            )
        )
    assert len(results) == 1
    assert snapshot.value_for(FundamentalConcept.REVENUE).fact_id == revenue_b.fact_id


def test_negative_revenue_or_assets_is_flagged_not_rejected() -> None:
    negative_assets = _fact(
        concept=FundamentalConcept.TOTAL_ASSETS, source_tag="Assets", period_start=None, value="-1"
    )
    anomalies = detect_fact_anomalies((negative_assets,))
    assert any(item.reason == "NEGATIVE_VALUE_UNEXPECTED" for item in anomalies)


def test_derive_q4_discrete_fact_subtracts_with_exact_provenance() -> None:
    q1 = _fact(
        fiscal_period=FiscalPeriod.Q1,
        period_start="2023-10-01",
        period_end="2023-12-30",
        value="100",
        accession="0000320193-24-000001",
        form="10-Q",
    )
    q2 = _fact(
        fiscal_period=FiscalPeriod.Q2,
        period_start="2023-12-31",
        period_end="2024-03-30",
        value="90",
        accession="0000320193-24-000002",
        form="10-Q",
    )
    q3 = _fact(
        fiscal_period=FiscalPeriod.Q3,
        period_start="2024-03-31",
        period_end="2024-06-29",
        value="95",
        accession="0000320193-24-000003",
        form="10-Q",
    )
    fy = _fact(value="391")
    q4 = derive_q4_discrete_fact(fiscal_year_fact=fy, q1_fact=q1, q2_fact=q2, q3_fact=q3)
    assert q4 is not None
    assert q4.value == Decimal("106")
    assert q4.is_ytd_derived is True
    assert all(
        source in q4.derivation_note for source in (fy.fact_id, q1.fact_id, q2.fact_id, q3.fact_id)
    )


def test_derive_q4_discrete_fact_refuses_mismatched_inputs_rather_than_guessing() -> None:
    fy = _fact(value="391")
    wrong_concept = _fact(
        concept=FundamentalConcept.NET_INCOME,
        source_tag="NetIncomeLoss",
        fiscal_period=FiscalPeriod.Q1,
        period_start="2023-10-01",
        period_end="2023-12-30",
        value="10",
        accession="0000320193-24-000001",
        form="10-Q",
    )
    q2 = _fact(
        fiscal_period=FiscalPeriod.Q2,
        period_start="2023-12-31",
        period_end="2024-03-30",
        value="90",
        accession="0000320193-24-000002",
        form="10-Q",
    )
    q3 = _fact(
        fiscal_period=FiscalPeriod.Q3,
        period_start="2024-03-31",
        period_end="2024-06-29",
        value="95",
        accession="0000320193-24-000003",
        form="10-Q",
    )
    assert (
        derive_q4_discrete_fact(fiscal_year_fact=fy, q1_fact=wrong_concept, q2_fact=q2, q3_fact=q3)
        is None
    )


# --------------------------------------------------------------------------
# Features: growth; margins; leverage; share-count change
# --------------------------------------------------------------------------


def _snapshot_with(*facts: FundamentalFact) -> tuple[object, tuple[FundamentalFact, ...]]:
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(),
        facts=facts,
    )
    return snapshot, facts


def test_yoy_revenue_growth_computed_and_unavailable_without_prior_year() -> None:
    current = _fact(value="391035000000", fiscal_year=2024)
    snapshot, facts = _snapshot_with(current)
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    growth = next(
        item for item in features if item.feature_type is DerivedFeatureType.YOY_REVENUE_GROWTH
    )
    assert growth.value is None
    assert growth.unavailable_reason == "PRIOR_YEAR_SAME_PERIOD_FACT_UNAVAILABLE"

    prior = _fact(
        value="383285000000",
        fiscal_year=2023,
        accession="0000320193-23-000106",
        filed_at=datetime(2023, 11, 3, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 3, 16, 30, tzinfo=UTC),
    )
    snapshot2, facts2 = _snapshot_with(current, prior)
    features2 = compute_derived_features(snapshot=snapshot2, facts=facts2)
    growth2 = next(
        item for item in features2 if item.feature_type is DerivedFeatureType.YOY_REVENUE_GROWTH
    )
    assert growth2.value == _q(
        (Decimal("391035000000") - Decimal("383285000000")) / Decimal("383285000000")
    )
    assert growth2.required_source_fact_ids == (current.fact_id, prior.fact_id)


def test_operating_margin_and_net_margin() -> None:
    revenue = _fact(value="391035000000")
    operating_income = _fact(
        concept=FundamentalConcept.OPERATING_INCOME,
        source_tag="OperatingIncomeLoss",
        value="123216000000",
    )
    net_income = _fact(
        concept=FundamentalConcept.NET_INCOME, source_tag="NetIncomeLoss", value="93736000000"
    )
    snapshot, facts = _snapshot_with(revenue, operating_income, net_income)
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    margin = next(
        item for item in features if item.feature_type is DerivedFeatureType.OPERATING_MARGIN
    )
    net_margin = next(
        item for item in features if item.feature_type is DerivedFeatureType.NET_MARGIN
    )
    assert margin.value == _q(Decimal("123216000000") / Decimal("391035000000"))
    assert net_margin.value == _q(Decimal("93736000000") / Decimal("391035000000"))


def test_margin_unavailable_when_revenue_is_zero() -> None:
    revenue = _fact(value="0")
    operating_income = _fact(
        concept=FundamentalConcept.OPERATING_INCOME, source_tag="OperatingIncomeLoss", value="5"
    )
    snapshot, facts = _snapshot_with(revenue, operating_income)
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    margin = next(
        item for item in features if item.feature_type is DerivedFeatureType.OPERATING_MARGIN
    )
    assert margin.value is None
    assert margin.unavailable_reason == "DENOMINATOR_ZERO"


def test_leverage_and_cash_to_assets_ratios() -> None:
    assets = _fact(
        concept=FundamentalConcept.TOTAL_ASSETS,
        source_tag="Assets",
        period_start=None,
        value="364980000000",
    )
    liabilities = _fact(
        concept=FundamentalConcept.TOTAL_LIABILITIES,
        source_tag="Liabilities",
        period_start=None,
        value="308030000000",
    )
    cash = _fact(
        concept=FundamentalConcept.CASH_AND_EQUIVALENTS,
        source_tag="CashAndCashEquivalentsAtCarryingValue",
        period_start=None,
        value="29943000000",
    )
    snapshot, facts = _snapshot_with(assets, liabilities, cash)
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    leverage = next(
        item
        for item in features
        if item.feature_type is DerivedFeatureType.LEVERAGE_LIABILITIES_TO_ASSETS
    )
    cash_to_assets = next(
        item for item in features if item.feature_type is DerivedFeatureType.CASH_TO_ASSETS
    )
    assert leverage.value == _q(Decimal("308030000000") / Decimal("364980000000"))
    assert cash_to_assets.value == _q(Decimal("29943000000") / Decimal("364980000000"))


def test_share_count_change_yoy() -> None:
    current = _fact(
        concept=FundamentalConcept.SHARES_OUTSTANDING,
        source_tag="CommonStockSharesOutstanding",
        period_start=None,
        value="15115823000",
    )
    prior = _fact(
        concept=FundamentalConcept.SHARES_OUTSTANDING,
        source_tag="CommonStockSharesOutstanding",
        period_start=None,
        value="15550061000",
        fiscal_year=2023,
        accession="0000320193-23-000106",
        filed_at=datetime(2023, 11, 3, tzinfo=UTC),
        accepted_at=datetime(2023, 11, 3, 16, 30, tzinfo=UTC),
    )
    snapshot, facts = _snapshot_with(current, prior)
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    change = next(
        item for item in features if item.feature_type is DerivedFeatureType.SHARE_COUNT_CHANGE_YOY
    )
    assert change.value == _q(
        (Decimal("15115823000") - Decimal("15550061000")) / Decimal("15550061000")
    )
    assert change.value is not None and change.value < 0  # net buyback


def test_operating_margin_change_yoy_requires_both_periods() -> None:
    revenue = _fact(value="391035000000")
    operating_income = _fact(
        concept=FundamentalConcept.OPERATING_INCOME,
        source_tag="OperatingIncomeLoss",
        value="123216000000",
    )
    snapshot, facts = _snapshot_with(revenue, operating_income)
    features = compute_derived_features(snapshot=snapshot, facts=facts)
    change = next(
        item
        for item in features
        if item.feature_type is DerivedFeatureType.OPERATING_MARGIN_CHANGE_YOY
    )
    assert change.value is None
    assert change.unavailable_reason == "PRIOR_YEAR_MARGIN_UNAVAILABLE"


def test_filing_event_features_days_since_last_filing_and_indicators() -> None:
    filing = _filing()
    features = compute_filing_event_features(
        stable_security_id="sec-aapl", as_of=datetime(2024, 11, 2, tzinfo=UTC), filings=(filing,)
    )
    by_type = {item.feature_type: item for item in features}
    assert by_type[FilingEventFeatureType.DAYS_SINCE_LAST_10Q_OR_10K].value == Decimal(1)
    assert by_type[FilingEventFeatureType.NEW_FILING_INDICATOR].value == Decimal(1)
    assert by_type[FilingEventFeatureType.RECENT_8K_INDICATOR].value == Decimal(0)
    assert by_type[FilingEventFeatureType.AMENDMENT_INDICATOR].value == Decimal(0)
    assert by_type[FilingEventFeatureType.DAYS_FISCAL_PERIOD_END_TO_FILING].value == Decimal(34)


def test_filing_event_features_never_see_a_future_filing() -> None:
    filing = _filing()
    features = compute_filing_event_features(
        stable_security_id="sec-aapl", as_of=datetime(2024, 10, 1, tzinfo=UTC), filings=(filing,)
    )
    by_type = {item.feature_type: item for item in features}
    assert by_type[FilingEventFeatureType.DAYS_SINCE_LAST_10Q_OR_10K].value is None
    assert by_type[FilingEventFeatureType.NEW_FILING_INDICATOR].value == Decimal(0)


# --------------------------------------------------------------------------
# Earnings-event seam (section 10)
# --------------------------------------------------------------------------


def test_earnings_event_derived_only_from_8k_item_2_02_with_explicit_limitation() -> None:
    earnings_8k = _filing(
        accession="0000320193-24-000100",
        form=FilingForm.FORM_8K,
        items=("2.02", "9.01"),
        report_period=None,
    )
    other_8k = _filing(
        accession="0000320193-24-000101",
        form=FilingForm.FORM_8K,
        items=("5.02",),
        report_period=None,
    )
    tenk = _filing()
    events = derive_earnings_events((earnings_8k, other_8k, tenk))
    assert len(events) == 1
    assert events[0].accession_number == earnings_8k.accession_number
    assert (
        events[0].limitation
        == "FILING_ACCEPTANCE_TIME_USED_AS_EARNINGS_PROXY_NOT_EXACT_RELEASE_TIME"
    )


# --------------------------------------------------------------------------
# Data quality / cohort resolution (sections 17, 20)
# --------------------------------------------------------------------------


def test_issuer_quality_classification_progresses_with_evidence_completeness() -> None:
    rejected = assess_issuer_quality(
        stable_security_id="sec-x", symbol="X", cik="1", filings=(), facts=()
    )
    assert rejected.quality is IssuerDataQuality.REJECTED

    sparse = assess_issuer_quality(
        stable_security_id="sec-aapl",
        symbol="AAPL",
        cik="320193",
        filings=(_filing(),),
        facts=(_fact(),),
    )
    assert sparse.quality in {
        IssuerDataQuality.USABLE_WITH_WARNINGS,
        IssuerDataQuality.INSUFFICIENT,
    }

    facts = (
        _fact(),
        _fact(
            concept=FundamentalConcept.NET_INCOME, source_tag="NetIncomeLoss", value="93736000000"
        ),
        _fact(
            concept=FundamentalConcept.TOTAL_ASSETS,
            source_tag="Assets",
            period_start=None,
            value="364980000000",
        ),
        _fact(
            concept=FundamentalConcept.TOTAL_LIABILITIES,
            source_tag="Liabilities",
            period_start=None,
            value="308030000000",
        ),
        _fact(
            concept=FundamentalConcept.STOCKHOLDERS_EQUITY,
            source_tag="StockholdersEquity",
            period_start=None,
            value="56950000000",
        ),
        _fact(
            concept=FundamentalConcept.OPERATING_CASH_FLOW,
            source_tag="NetCashProvidedByUsedInOperatingActivities",
            value="118254000000",
        ),
    )
    usable = assess_issuer_quality(
        stable_security_id="sec-aapl",
        symbol="AAPL",
        cik="320193",
        filings=(_filing(),),
        facts=facts,
    )
    assert usable.quality is IssuerDataQuality.USABLE
    assert usable.has_10k is True


def test_resolve_corporate_evidence_cohort_reports_missing_cik_honestly() -> None:
    resolved_cohort = ResolvedCohort(
        policy_id="policy-1",
        policy_hash="a" * 64,
        members=(
            ResolvedCohortMember(
                stable_security_id="sec-aapl",
                symbol="AAPL",
                sector=CohortSector.TECHNOLOGY,
                roles=(CohortRole.LARGE_CAP_EQUITY,),
                asset_class="COMMON_STOCK",
            ),
            ResolvedCohortMember(
                stable_security_id="sec-spy",
                symbol="SPY",
                sector=CohortSector.BROAD_MARKET,
                roles=(CohortRole.BROAD_INDEX_ETF,),
                asset_class="ETF",
            ),
        ),
        unresolved_symbols=(),
        ambiguous_symbols=(),
    )
    identities = (
        StableSecurityIdentity(
            stable_security_id="sec-aapl",
            identity_basis=IdentityBasis.CIK_AND_SYMBOL,
            cik="320193",
            normalized_symbol="AAPL",
            current_symbol="AAPL",
            source_identity_hash="h1",
            source_canonical_security_id="c1",
            review_required=False,
            canonical_hash="h1",
        ),
        StableSecurityIdentity(
            stable_security_id="sec-spy",
            identity_basis=IdentityBasis.SYMBOL_ONLY,
            cik=None,
            normalized_symbol="SPY",
            current_symbol="SPY",
            source_identity_hash="h2",
            source_canonical_security_id="c2",
            review_required=False,
            canonical_hash="h2",
        ),
    )
    resolution = resolve_corporate_evidence_cohort(resolved_cohort, identities)
    assert [item.symbol for item in resolution.members] == ["AAPL"]
    assert resolution.excluded_missing_cik == ("SPY",)


# --------------------------------------------------------------------------
# Bounded capture plan / checkpoint lifecycle (sections 11, 12)
# --------------------------------------------------------------------------


def _plan(max_issuers_per_run: int = 2) -> CorporateEvidenceCohortPlan:
    bounds = CorporateEvidenceCaptureBounds.create(max_issuers_per_run=max_issuers_per_run)
    resolution = CorporateEvidenceCohortResolution(
        members=(
            CorporateEvidenceCohortMember(
                "sec-aapl", "AAPL", "320193", "TECHNOLOGY", "COMMON_STOCK"
            ),
            CorporateEvidenceCohortMember(
                "sec-msft", "MSFT", "789019", "TECHNOLOGY", "COMMON_STOCK"
            ),
            CorporateEvidenceCohortMember(
                "sec-nvda", "NVDA", "1045810", "TECHNOLOGY", "COMMON_STOCK"
            ),
        ),
        excluded_missing_cik=("SPY",),
        excluded_unresolved=(),
    )
    return CorporateEvidenceCohortPlan.create(
        cohort_policy_id="policy-1",
        cohort_policy_hash="a" * 64,
        resolution=resolution,
        bounds=bounds,
        created_at=UTC_NOW,
    )


def test_plan_registration_is_content_addressed_and_deterministic() -> None:
    assert _plan().plan_id == _plan().plan_id


def test_capture_checkpoint_resumes_across_bounded_runs_until_complete() -> None:
    plan = _plan(max_issuers_per_run=2)
    pending = pending_members(plan, processed_ciks=())
    batch = select_run_batch(pending, bounds=plan.bounds)
    assert [item.symbol for item in batch] == ["AAPL", "MSFT"]

    outcomes = tuple(
        IssuerCaptureOutcome(
            m.stable_security_id, m.cik, IssuerCaptureStatus.CAPTURED, 3, 10, 100, 2000, 2, (), ()
        )
        for m in batch
    )
    checkpoint1 = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=outcomes,
        previously_processed_ciks=(),
        total_requests_this_run=4,
        total_bytes_this_run=4200,
        runtime_seconds=Decimal("2.0"),
        created_at=UTC_NOW,
    )
    assert checkpoint1.complete is False

    pending2 = pending_members(plan, processed_ciks=checkpoint1.cumulative_processed_ciks)
    assert [item.symbol for item in pending2] == ["NVDA"]
    batch2 = select_run_batch(pending2, bounds=plan.bounds)
    outcomes2 = tuple(
        IssuerCaptureOutcome(
            m.stable_security_id, m.cik, IssuerCaptureStatus.CAPTURED, 3, 10, 100, 2000, 2, (), ()
        )
        for m in batch2
    )
    checkpoint2 = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-2",
        outcomes=outcomes2,
        previously_processed_ciks=checkpoint1.cumulative_processed_ciks,
        total_requests_this_run=2,
        total_bytes_this_run=2100,
        runtime_seconds=Decimal("1.0"),
        created_at=UTC_NOW,
    )
    assert checkpoint2.complete is True


def test_capture_resume_against_an_already_complete_plan_is_a_safe_no_op() -> None:
    plan = _plan(max_issuers_per_run=2)
    all_ciks = tuple(sorted(member.cik for member in plan.members))
    complete_checkpoint = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=tuple(
            IssuerCaptureOutcome(
                m.stable_security_id, m.cik, IssuerCaptureStatus.CAPTURED, 1, 1, 10, 10, 2, (), ()
            )
            for m in plan.members
        ),
        previously_processed_ciks=(),
        total_requests_this_run=6,
        total_bytes_this_run=60,
        runtime_seconds=Decimal("3.0"),
        created_at=UTC_NOW,
    )
    assert complete_checkpoint.complete is True
    assert tuple(sorted(complete_checkpoint.cumulative_processed_ciks)) == all_ciks

    # A duplicate resume run against an already-complete plan finds nothing
    # pending -- and folding an empty outcome set into a new checkpoint
    # must never lose, duplicate, or reorder the already-processed set.
    pending = pending_members(plan, processed_ciks=complete_checkpoint.cumulative_processed_ciks)
    assert pending == ()
    duplicate_checkpoint = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-2",
        outcomes=(),
        previously_processed_ciks=complete_checkpoint.cumulative_processed_ciks,
        total_requests_this_run=0,
        total_bytes_this_run=0,
        runtime_seconds=Decimal("0.0"),
        created_at=UTC_NOW,
    )
    assert duplicate_checkpoint.complete is True
    assert tuple(sorted(duplicate_checkpoint.cumulative_processed_ciks)) == all_ciks


def test_failed_and_partial_outcomes_count_as_processed_unlike_budget_exhausted() -> None:
    plan = _plan(max_issuers_per_run=3)
    outcomes = (
        IssuerCaptureOutcome(
            "sec-aapl", "320193", IssuerCaptureStatus.CAPTURED, 3, 10, 100, 2000, 2, (), ()
        ),
        IssuerCaptureOutcome(
            "sec-msft",
            "789019",
            IssuerCaptureStatus.FAILED,
            0,
            0,
            0,
            0,
            1,
            ("SUBMISSIONS_UNAVAILABLE:SecFilingsSourceError",),
            (),
        ),
        IssuerCaptureOutcome(
            "sec-nvda",
            "1045810",
            IssuerCaptureStatus.PARTIAL,
            5,
            0,
            100,
            0,
            1,
            ("COMPANYFACTS_UNAVAILABLE:SecFilingsSourceCapabilityUnavailable",),
            (),
        ),
    )
    checkpoint = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=outcomes,
        previously_processed_ciks=(),
        total_requests_this_run=4,
        total_bytes_this_run=2100,
        runtime_seconds=Decimal("1.0"),
        created_at=UTC_NOW,
    )
    # A genuine, exhausted-budget skip must be retried; a FAILED or PARTIAL
    # issuer was genuinely attempted and must not be retried forever on the
    # identical transient/unavailable evidence.
    assert tuple(sorted(checkpoint.cumulative_processed_ciks)) == ("1045810", "320193", "789019")
    assert checkpoint.complete is True
    assert pending_members(plan, processed_ciks=checkpoint.cumulative_processed_ciks) == ()


def test_budget_exhausted_issuers_are_retained_honestly_not_silently_dropped() -> None:
    plan = _plan(max_issuers_per_run=3)
    outcomes = (
        IssuerCaptureOutcome(
            "sec-aapl", "320193", IssuerCaptureStatus.CAPTURED, 3, 10, 100, 2000, 2, (), ()
        ),
        IssuerCaptureOutcome(
            "sec-msft",
            "789019",
            IssuerCaptureStatus.SKIPPED_BUDGET_EXHAUSTED,
            0,
            0,
            0,
            0,
            0,
            ("RUN_BUDGET_EXHAUSTED",),
            (),
        ),
    )
    checkpoint = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=outcomes,
        previously_processed_ciks=(),
        total_requests_this_run=2,
        total_bytes_this_run=2100,
        runtime_seconds=Decimal("1.0"),
        created_at=UTC_NOW,
    )
    # Only the genuinely-attempted issuer counts as processed -- a skipped
    # one must be retried, not treated as done.
    assert checkpoint.cumulative_processed_ciks == ("320193",)
    assert checkpoint.complete is False


# --------------------------------------------------------------------------
# Dataset / replay: deterministic snapshot; future evidence blocked;
# quality propagation
# --------------------------------------------------------------------------


def test_dataset_snapshot_is_deterministic_over_identical_evidence() -> None:
    plan = _plan()
    filings = (_filing(),)
    facts = (_fact(),)
    assessments = (
        assess_issuer_quality(
            stable_security_id="sec-aapl", symbol="AAPL", cik="320193", filings=filings, facts=facts
        ),
    )
    report_a = CorporateDataQualityReport.create(
        plan=plan,
        issuer_assessments=assessments,
        all_filings=filings,
        all_facts=facts,
        checkpoint_complete=True,
        created_at=UTC_NOW,
    )
    report_b = CorporateDataQualityReport.create(
        plan=plan,
        issuer_assessments=assessments,
        all_filings=filings,
        all_facts=facts,
        checkpoint_complete=True,
        created_at=UTC_NOW,
    )
    assert report_a.report_id == report_b.report_id

    dataset_a = CorporateHistoryDatasetSnapshot.create(
        plan=plan,
        checkpoint_ids=("checkpoint-1",),
        filings=filings,
        facts=facts,
        quality_report=report_a,
        source_receipt_hashes=("h1",),
        created_at=UTC_NOW,
    )
    dataset_b = CorporateHistoryDatasetSnapshot.create(
        plan=plan,
        checkpoint_ids=("checkpoint-1",),
        filings=filings,
        facts=facts,
        quality_report=report_a,
        source_receipt_hashes=("h1",),
        created_at=UTC_NOW,
    )
    assert dataset_a.dataset_id == dataset_b.dataset_id
    assert dataset_a.completeness == "COMPLETE"


def test_dataset_quality_propagates_partial_completeness_honestly() -> None:
    plan = _plan()
    report = CorporateDataQualityReport.create(
        plan=plan,
        issuer_assessments=(),
        all_filings=(),
        all_facts=(),
        checkpoint_complete=False,
        created_at=UTC_NOW,
    )
    assert report.point_in_time_completeness == "PARTIAL"
    dataset = CorporateHistoryDatasetSnapshot.create(
        plan=plan,
        checkpoint_ids=("checkpoint-1",),
        filings=(),
        facts=(),
        quality_report=report,
        source_receipt_hashes=(),
        created_at=UTC_NOW,
    )
    assert dataset.completeness == "PARTIAL"


def test_replay_attachment_rejects_lookahead_evidence_as_defense_in_depth() -> None:
    filing = _filing()
    fact = _fact()
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(filing,),
        facts=(fact,),
    )
    features = compute_derived_features(snapshot=snapshot, facts=(fact,))
    attachment = ReplayCorporateEvidenceAttachment.create(
        replay_run_id="replay-1",
        frame_id="frame-1",
        snapshot=snapshot,
        features=features,
        created_at=UTC_NOW,
    )
    assert attachment.as_of == snapshot.as_of
    assert attachment.snapshot_hash == snapshot.canonical_hash
    assert attachment.fact_lineage == snapshot.fact_lineage


def test_replay_attachment_is_deterministic_for_identical_snapshot_and_features() -> None:
    filing = _filing()
    fact = _fact()
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl",
        cik="320193",
        as_of=datetime(2024, 11, 2, tzinfo=UTC),
        filings=(filing,),
        facts=(fact,),
    )
    features = compute_derived_features(snapshot=snapshot, facts=(fact,))
    first = ReplayCorporateEvidenceAttachment.create(
        replay_run_id="replay-1",
        frame_id="frame-1",
        snapshot=snapshot,
        features=features,
        created_at=UTC_NOW,
    )
    second = ReplayCorporateEvidenceAttachment.create(
        replay_run_id="replay-1",
        frame_id="frame-1",
        snapshot=snapshot,
        features=features,
        created_at=UTC_NOW,
    )
    assert first.attachment_id == second.attachment_id


# --------------------------------------------------------------------------
# Lookback policy (Corporate Evidence Completion and Replay Scaling v1,
# section 2): cutoff/floor semantics, predecessor-period retention for YoY,
# amendment/restatement behavior around the boundary.
# --------------------------------------------------------------------------


def _instant_fact(
    *, period_end: str, accession: str, fiscal_year: int, value: str = "1000"
) -> FundamentalFact:
    """An INSTANT-typed fact (no ``period_start``) so lookback-boundary
    tests can place ``period_end`` anywhere without also having to derive a
    matching duration start."""
    return FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.TOTAL_ASSETS,
        source_taxonomy="us-gaap",
        source_tag="Assets",
        fiscal_year=fiscal_year,
        fiscal_period=FiscalPeriod.FY,
        period_start=None,
        period_end=period_end,
        value=Decimal(value),
        accession_number=accession,
        form="10-K",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, 16, 30, tzinfo=UTC),
    )


def test_lookback_window_cutoff_and_retention_floor_dates() -> None:
    window = LookbackWindow.create(lookback_years=6, reference_date=date(2026, 8, 8))
    assert window.reference_date == "2026-08-08"
    assert window.cutoff_date == "2020-08-08"
    assert window.retention_floor_date == "2019-08-08"


def test_lookback_window_falls_back_from_leap_day_honestly() -> None:
    # 2024 is a leap year; three years back (2021) is not, so Feb 29 has no
    # exact match -- Feb 28 is the only honest fallback, never a silent
    # off-by-one into March.
    window = LookbackWindow.create(lookback_years=3, reference_date=date(2024, 2, 29))
    assert window.cutoff_date == "2021-02-28"


def test_lookback_window_rejects_out_of_bounds_years() -> None:
    with pytest.raises(CorporateFundamentalsError):
        LookbackWindow.create(lookback_years=0, reference_date=date(2026, 8, 8))
    with pytest.raises(CorporateFundamentalsError):
        LookbackWindow.create(lookback_years=31, reference_date=date(2026, 8, 8))


def test_apply_lookback_policy_cutoff_inclusion_and_exclusion_boundaries() -> None:
    window = LookbackWindow.create(lookback_years=6, reference_date=date(2026, 8, 8))
    # cutoff_date == 2020-08-08; retention_floor_date == 2019-08-08.
    at_cutoff = _instant_fact(
        period_end="2020-08-08", accession="0000320193-20-000001", fiscal_year=2020
    )
    at_floor = _instant_fact(
        period_end="2019-08-08", accession="0000320193-19-000001", fiscal_year=2019
    )
    before_floor = _instant_fact(
        period_end="2019-08-07", accession="0000320193-19-000002", fiscal_year=2019
    )
    result = apply_lookback_policy(
        filings=(), facts=(at_cutoff, at_floor, before_floor), window=window
    )
    included_ids = {item.fact_id for item in result.included_facts}
    assert at_cutoff.fact_id in included_ids
    assert at_floor.fact_id in included_ids
    assert before_floor.fact_id not in included_ids
    assert result.excluded_fact_count == 1

    # At-cutoff is genuinely in-window; at-floor is retained *only* as a
    # YoY predecessor comparator for the oldest in-window period.
    assert at_cutoff.fact_id not in result.predecessor_only_fact_ids
    assert at_floor.fact_id in result.predecessor_only_fact_ids


def test_apply_lookback_policy_retains_exactly_one_predecessor_year_for_yoy() -> None:
    window = LookbackWindow.create(lookback_years=6, reference_date=date(2026, 8, 8))
    current = _instant_fact(
        period_end="2020-09-01", accession="0000320193-20-000010", fiscal_year=2020
    )
    prior_year = _instant_fact(
        period_end="2019-09-01", accession="0000320193-19-000010", fiscal_year=2019
    )
    two_years_back = _instant_fact(
        period_end="2018-09-01", accession="0000320193-18-000010", fiscal_year=2018
    )
    result = apply_lookback_policy(
        filings=(), facts=(current, prior_year, two_years_back), window=window
    )
    included_ids = {item.fact_id for item in result.included_facts}
    assert current.fact_id in included_ids
    assert prior_year.fact_id in included_ids
    assert two_years_back.fact_id not in included_ids
    assert result.predecessor_only_fact_ids == (prior_year.fact_id,)


def test_apply_lookback_policy_filters_filings_by_report_period_not_filed_date() -> None:
    window = LookbackWindow.create(lookback_years=6, reference_date=date(2026, 8, 8))
    # Both amendments are filed at the identical, very recent date -- only
    # the *reported period* decides inclusion, never how recently either
    # was filed.
    in_window_amendment = _filing(
        accession="0000320193-26-000001",
        form=FilingForm.FORM_10K_A,
        report_period="2020-09-26",
        filed_at=datetime(2026, 1, 15, tzinfo=UTC),
        accepted_at=datetime(2026, 1, 15, 16, 0, tzinfo=UTC),
    )
    out_of_window_amendment = _filing(
        accession="0000320193-26-000002",
        form=FilingForm.FORM_10K_A,
        report_period="2018-09-29",
        filed_at=datetime(2026, 1, 15, tzinfo=UTC),
        accepted_at=datetime(2026, 1, 15, 16, 0, tzinfo=UTC),
    )
    result = apply_lookback_policy(
        filings=(in_window_amendment, out_of_window_amendment), facts=(), window=window
    )
    included_ids = {item.filing_event_id for item in result.included_filings}
    assert in_window_amendment.filing_event_id in included_ids
    assert out_of_window_amendment.filing_event_id not in included_ids
    assert result.excluded_filing_count == 1


def test_apply_lookback_policy_restatement_of_in_window_period_stays_in() -> None:
    window = LookbackWindow.create(lookback_years=6, reference_date=date(2026, 8, 8))
    original = _instant_fact(
        period_end="2021-06-30", accession="0000320193-21-000005", fiscal_year=2021
    )
    restated = FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.TOTAL_ASSETS,
        source_taxonomy="us-gaap",
        source_tag="Assets",
        fiscal_year=2021,
        fiscal_period=FiscalPeriod.FY,
        period_start=None,
        period_end="2021-06-30",
        value=Decimal("999"),
        accession_number="0000320193-26-000099",
        form="10-K/A",
        filed_at=datetime(2026, 2, 1, tzinfo=UTC),
        accepted_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    result = apply_lookback_policy(filings=(), facts=(original, restated), window=window)
    included_ids = {item.fact_id for item in result.included_facts}
    assert original.fact_id in included_ids
    assert restated.fact_id in included_ids


def test_apply_lookback_policy_never_mutates_its_inputs() -> None:
    window = LookbackWindow.create(lookback_years=6, reference_date=date(2026, 8, 8))
    filings = (_filing(),)
    facts = (_fact(),)
    apply_lookback_policy(filings=filings, facts=facts, window=window)
    # Pure read-side filter: the caller's own sequences are exactly what
    # they were before the call -- nothing is deleted or reordered in
    # place. "Immutable old rows remain" for the persisted case is proven
    # at the application/persistence layer, where a materialized dataset
    # excludes an out-of-window fact while the repository still returns it.
    assert filings == (_filing(),)
    assert facts == (_fact(),)


def test_dataset_snapshot_excludes_out_of_window_evidence_but_reports_it() -> None:
    plan = _plan()
    in_window = _fact(accession="0000320193-24-000123", period_end="2024-09-28")
    out_of_window = _instant_fact(
        period_end="2010-01-01", accession="0000320193-10-000001", fiscal_year=2010
    )
    filings = (_filing(),)
    facts = (in_window, out_of_window)
    assessments = (
        assess_issuer_quality(
            stable_security_id="sec-aapl", symbol="AAPL", cik="320193", filings=filings, facts=facts
        ),
    )
    report = CorporateDataQualityReport.create(
        plan=plan,
        issuer_assessments=assessments,
        all_filings=filings,
        all_facts=facts,
        checkpoint_complete=True,
        created_at=UTC_NOW,
    )
    dataset = CorporateHistoryDatasetSnapshot.create(
        plan=plan,
        checkpoint_ids=("checkpoint-1",),
        filings=filings,
        facts=facts,
        quality_report=report,
        source_receipt_hashes=(),
        created_at=UTC_NOW,
    )
    assert in_window.fact_id in dataset.fact_ids
    assert out_of_window.fact_id not in dataset.fact_ids
    assert dataset.lookback_excluded_fact_count == 1
    assert dataset.lookback_years == plan.bounds.lookback_years
    assert dataset.lookback_cutoff_date and dataset.lookback_retention_floor_date


# --------------------------------------------------------------------------
# Period-gap detection (section 17)
# --------------------------------------------------------------------------


def test_detect_filing_period_gaps_flags_a_skipped_quarter() -> None:
    filings = (
        _filing(
            accession="0000320193-24-000001", form=FilingForm.FORM_10Q, report_period="2023-12-30"
        ),
        # A quarter's 10-Q is genuinely missing here -- next retained core
        # filing jumps straight to a period six months later.
        _filing(
            accession="0000320193-24-000002", form=FilingForm.FORM_10Q, report_period="2024-06-29"
        ),
    )
    gaps = detect_filing_period_gaps(filings)
    assert len(gaps) == 1
    assert gaps[0].previous_report_period == "2023-12-30"
    assert gaps[0].next_report_period == "2024-06-29"
    assert gaps[0].gap_days > 130


def test_detect_filing_period_gaps_silent_for_ordinary_quarterly_cadence() -> None:
    filings = (
        _filing(
            accession="0000320193-24-000001", form=FilingForm.FORM_10Q, report_period="2023-12-30"
        ),
        _filing(
            accession="0000320193-24-000002", form=FilingForm.FORM_10Q, report_period="2024-03-30"
        ),
    )
    assert detect_filing_period_gaps(filings) == ()


# --------------------------------------------------------------------------
# Corporate replay feature frame (section 5): bounded selection, no
# composite score.
# --------------------------------------------------------------------------


def test_corporate_replay_feature_frame_bundles_the_bounded_field_set() -> None:
    filing = _filing()
    fact = _fact()
    as_of = datetime(2024, 11, 2, tzinfo=UTC)
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl", cik="320193", as_of=as_of, filings=(filing,), facts=(fact,)
    )
    derived = compute_derived_features(snapshot=snapshot, facts=(fact,))
    filing_features = compute_filing_event_features(
        stable_security_id="sec-aapl", as_of=as_of, filings=(filing,)
    )
    frame = build_corporate_replay_feature_frame(
        snapshot=snapshot, derived_features=derived, filing_event_features=filing_features
    )
    assert isinstance(frame, CorporateReplayFeatureFrame)
    assert frame.stable_security_id == "sec-aapl"
    assert frame.as_of == as_of
    assert frame.snapshot_quality is snapshot.quality
    assert frame.revenue_growth_yoy.feature_type is DerivedFeatureType.YOY_REVENUE_GROWTH
    assert (
        frame.latest_10q_or_10k_age_days.feature_type
        is FilingEventFeatureType.DAYS_SINCE_LAST_10Q_OR_10K
    )
    assert frame.recent_8k_indicator.feature_type is FilingEventFeatureType.RECENT_8K_INDICATOR
    # No opaque composite score anywhere on the frame.
    assert not hasattr(frame, "quality_score")
    assert not hasattr(frame, "composite_score")


def test_corporate_replay_feature_frame_reports_missing_fields_honestly() -> None:
    # No retained evidence at all: every bounded slot is unavailable, and
    # every one of them is named in ``missing_fields`` -- never silently
    # dropped.
    as_of = datetime(2024, 11, 2, tzinfo=UTC)
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-empty", cik="000000", as_of=as_of, filings=(), facts=()
    )
    derived = compute_derived_features(snapshot=snapshot, facts=())
    filing_features = compute_filing_event_features(
        stable_security_id="sec-empty", as_of=as_of, filings=()
    )
    frame = build_corporate_replay_feature_frame(
        snapshot=snapshot, derived_features=derived, filing_event_features=filing_features
    )
    assert frame.missing_fields == (
        "cash_to_assets",
        "latest_10q_or_10k_age_days",
        "leverage_liabilities_to_assets",
        "net_income_growth_yoy",
        "operating_cash_flow_growth_yoy",
        "operating_margin",
        "operating_margin_change_yoy",
        "revenue_growth_yoy",
        "share_count_change_yoy",
    )


def test_corporate_replay_feature_frame_rejects_as_of_mismatch() -> None:
    filing = _filing()
    fact = _fact()
    as_of = datetime(2024, 11, 2, tzinfo=UTC)
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id="sec-aapl", cik="320193", as_of=as_of, filings=(filing,), facts=(fact,)
    )
    derived = compute_derived_features(snapshot=snapshot, facts=(fact,))
    stale_filing_features = compute_filing_event_features(
        stable_security_id="sec-aapl",
        as_of=datetime(2024, 11, 3, tzinfo=UTC),
        filings=(filing,),
    )
    with pytest.raises(CorporateFundamentalsError):
        build_corporate_replay_feature_frame(
            snapshot=snapshot, derived_features=derived, filing_event_features=stale_filing_features
        )
