"""Tests for Counterfactual Outcome Feedback v1 (``domain.counterfactual_outcome``)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.counterfactual_classification import (
    REASON_CLASSIFIER_V1,
    REASON_CLASSIFIER_V2,
    CounterfactualDecisionClassification,
    ReasonFamily,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES,
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V2,
    COUNTERFACTUAL_OUTCOME_POLICY_V3,
    KNOWN_COUNTERFACTUAL_OUTCOME_POLICY_VERSIONS,
    CounterfactualOutcomeError,
    CounterfactualOutcomeQualificationState,
    build_counterfactual_outcome_aggregates,
    counterfactual_outcome_aggregate_canonical_hash,
    counterfactual_outcome_aggregate_id,
    counterfactual_outcome_cohort_id,
)
from apex.domain.kalshi_baseline_quote import (
    BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    KALSHI_BASELINE_QUOTE_POLICY_V1,
    THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
    KalshiBaselineLegRole,
    KalshiBaselineQuoteEvidence,
    KalshiBaselineQuoteLeg,
    SettlementValueBasis,
    kalshi_baseline_quote_canonical_hash,
    kalshi_baseline_quote_id,
    reduce_kalshi_binary_complement_outcome,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import ValueValidationError, canonical_decimal

_NOW = datetime(2026, 8, 19, tzinfo=UTC)
_PROJECTION_ID = "projection-1"
_METHOD_ID = "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"
_RUN_ID = "cycle-2026-08-19-counterfactual-outcome"

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _decision(
    *,
    decision_id: str,
    venue: SharedVenue = SharedVenue.CRYPTO,
    projection_id: str = _PROJECTION_ID,
    disposition: DecisionDisposition = DecisionDisposition.NOT_ADVANCED,
    subject_source_record_type: str = "CryptoResearchOpportunity",
) -> CounterfactualDecision:
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        origin_decision_id=f"{decision_id}-origin",
        origin_canonical_hash="a" * 64,
        origin_state="REJECTED",
        subject_projection_id=projection_id,
        subject_source_record_type=subject_source_record_type,
        subject_source_record_id=f"{decision_id}-record",
        venue=venue,
        opportunity_set_id="set-1",
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=_NOW,
        disposition=disposition,
        raw_reason_codes=("SOURCE:ECONOMICS_TEST",),
        explanation="test decision",
        canonical_hash="b" * 64,
        authority="NONE",
    )


def _classification(
    *,
    decision_id: str,
    reason_families: tuple[ReasonFamily, ...],
    classifier_version: str = REASON_CLASSIFIER_V2,
) -> CounterfactualDecisionClassification:
    return CounterfactualDecisionClassification(
        classification_id=f"{decision_id}-{classifier_version}-classification",
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        classifier_version=classifier_version,
        classifier_policy_hash="c" * 64,
        reason_families=reason_families,
        classified_at=_NOW,
        canonical_hash="d" * 64,
        authority="NONE",
    )


def _observation(
    *,
    decision_id: str,
    venue: SharedVenue = SharedVenue.CRYPTO,
    horizon_kind: FollowUpHorizonKind = FollowUpHorizonKind.SHORT_HORIZON,
    baseline_kind: FollowUpBaselineKind = FollowUpBaselineKind.DECISION_TIME,
    policy_version: str = FOLLOWUP_POLICY_V2,
    subsequent_return: Decimal | None = Decimal("0.05"),
    target_evidence_class: EvidenceClass = EvidenceClass.GENUINE_PUBLIC,
) -> DecisionFollowUpObservation:
    observation_id = (
        f"{decision_id}-{horizon_kind.value}-{baseline_kind.value}-{policy_version}-obs"
    )
    is_crypto = venue is SharedVenue.CRYPTO
    observation_type = (
        FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT
        if is_crypto
        else FollowUpObservationType.KALSHI_MARKET_STATUS_AND_PRICE
    )
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        venue=venue,
        disposition=DecisionDisposition.NOT_ADVANCED,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        horizon_days=1,
        decision_evaluated_at=_NOW,
        baseline_time=_NOW,
        target_time=_NOW,
        observation_type=observation_type,
        subject_instrument_id="BTC-USD" if is_crypto else "KALSHI-EVT",
        subject_product_or_market_id="BTC-USD" if is_crypto else "KALSHI-MKT",
        provenance=target_evidence_class,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC if is_crypto else None,
        target_evidence_class=target_evidence_class,
        evidence_eligibility_policy_version=(
            "CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1" if is_crypto else None
        ),
        observed_at=_NOW,
        source_lineage=("batch-1",),
        data_limitations=(),
        reference_price_at_baseline=Decimal("100") if is_crypto else None,
        reference_price_at_target=Decimal("105") if is_crypto else None,
        subsequent_return=subsequent_return if is_crypto else None,
        market_status_at_target=None if is_crypto else "closed",
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None if is_crypto else "YES",
        settlement_source=None if is_crypto else "KALSHI_PUBLIC_RESULT_FIELD",
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=f"{observation_id}-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


def _assessment(
    *,
    observation: DecisionFollowUpObservation,
    verdict: FollowUpEvidenceQualityVerdict = FollowUpEvidenceQualityVerdict.ELIGIBLE,
    evidence_defect_type: FollowUpEvidenceDefectType = FollowUpEvidenceDefectType.NONE,
    assessment_policy_version: str = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
) -> FollowUpEvidenceQualityAssessment:
    return FollowUpEvidenceQualityAssessment(
        assessment_id=f"{observation.observation_id}-assessment",
        observation_id=observation.observation_id,
        observation_canonical_hash=observation.canonical_hash,
        assessment_policy_version=assessment_policy_version,
        baseline_evidence_class=observation.baseline_evidence_class,
        target_evidence_class=observation.target_evidence_class,
        verdict=verdict,
        reason_code="TEST_REASON",
        evidence_defect_type=evidence_defect_type,
        assessed_at=_NOW,
        canonical_hash=f"{observation.observation_id}-assessment-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


_UNSET = object()  # distinguishes "use _observation's own default" from "override to None"


def _cohort_of(
    n: int,
    *,
    returns: list[Decimal] | None = None,
    reason_family: ReasonFamily = ReasonFamily.ECONOMICS,
    venue: SharedVenue = SharedVenue.CRYPTO,
    strategy_or_method_id: str = _METHOD_ID,
    projection_id: str = _PROJECTION_ID,
    all_eligible: bool = True,
    decision_id_prefix: str = "decision",
    subject_source_record_type: str = "CryptoResearchOpportunity",
    settlement_result: str | None | object = _UNSET,
) -> tuple[
    tuple[CounterfactualDecision, ...],
    tuple[CounterfactualDecisionClassification, ...],
    tuple[DecisionFollowUpObservation, ...],
    tuple[FollowUpEvidenceQualityAssessment, ...],
    dict[str, str],
]:
    values = returns if returns is not None else [Decimal("0.01")] * n
    decisions: list[CounterfactualDecision] = []
    classifications: list[CounterfactualDecisionClassification] = []
    observations: list[DecisionFollowUpObservation] = []
    assessments: list[FollowUpEvidenceQualityAssessment] = []
    for index in range(n):
        decision_id = f"{decision_id_prefix}-{index}"
        decisions.append(
            _decision(
                decision_id=decision_id,
                venue=venue,
                projection_id=projection_id,
                subject_source_record_type=subject_source_record_type,
            )
        )
        classifications.append(
            _classification(decision_id=decision_id, reason_families=(reason_family,))
        )
        observation = _observation(
            decision_id=decision_id, venue=venue, subsequent_return=values[index]
        )
        if settlement_result is not _UNSET:
            observation = replace(observation, settlement_result=settlement_result)
        observations.append(observation)
        if all_eligible:
            assessments.append(_assessment(observation=observation))
    return (
        tuple(decisions),
        tuple(classifications),
        tuple(observations),
        tuple(assessments),
        {projection_id: strategy_or_method_id},
    )


def _only(aggregates: tuple, **kwargs: object):
    matches = [
        item
        for item in aggregates
        if all(getattr(item, key) == value for key, value in kwargs.items())
    ]
    assert len(matches) == 1, f"expected exactly one match for {kwargs}, got {len(matches)}"
    return matches[0]


_KALSHI_BASELINE_FEES = Decimal("0.06")


def _kalshi_baseline_quote(
    *,
    decision: CounterfactualDecision,
    total_modeled_acquisition_cost: Decimal = Decimal("9.76"),
    modeled_settlement_value: Decimal = Decimal("10"),
) -> KalshiBaselineQuoteEvidence:
    """A minimal, structurally valid ``KalshiBaselineQuoteEvidence`` for a
    ``BinaryComplementEvaluation``-sourced ``decision`` -- Kalshi Decision-
    Linked Baseline Quote Evidence v1's integration-level fixture, mirroring
    this file's existing ``_decision``/``_observation`` fixture style. Full
    leg-manifest/hash-determinism/leg-completeness coverage for the domain
    contract itself lives in ``tests/test_kalshi_baseline_quote.py``; this
    helper exists only to prove the aggregation layer (this module) consumes
    that evidence correctly."""

    gross_acquisition_cost = total_modeled_acquisition_cost - _KALSHI_BASELINE_FEES
    conservative_net_lower_bound = modeled_settlement_value - total_modeled_acquisition_cost
    leg = KalshiBaselineQuoteLeg(
        ordinal=0,
        leg_role=KalshiBaselineLegRole.SINGLE_MARKET_COMPLEMENT,
        market_ticker="KALSHI-MKT",
        purchased_side="YES",
        source_level_id="level-1",
        quantity=Decimal("10"),
        acquisition_price=gross_acquisition_cost / Decimal("10"),
        acquisition_cost=gross_acquisition_cost,
        modeled_fee=_KALSHI_BASELINE_FEES,
        rounding_reserve=Decimal("0"),
    )
    common = dict(
        quote_id=kalshi_baseline_quote_id(
            policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1, decision_id=decision.decision_id
        ),
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        subject_projection_id=decision.subject_projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        subject_source_canonical_hash="e" * 64,
        instrument_id="KALSHI-EVT",
        product_or_market_id="KALSHI-MKT",
        market_tickers=("KALSHI-MKT",),
        decision_evaluated_at=decision.evaluated_at,
        evaluation_observed_at=(decision.evaluated_at,),
        evaluator_version="public-binary-complement-v1",
        fee_model_identity="kalshi-quadratic-taker-v1",
        rounding_identity="test-rounding-v1",
        legs=(leg,),
        matched_quantity=Decimal("10"),
        gross_acquisition_cost=gross_acquisition_cost,
        total_modeled_fees=_KALSHI_BASELINE_FEES,
        total_modeled_acquisition_cost=total_modeled_acquisition_cost,
        conservative_net_lower_bound=conservative_net_lower_bound,
        modeled_settlement_value=modeled_settlement_value,
        settlement_value_basis=SettlementValueBasis.EXACT_COMPLEMENT_VALUE.value,
        source_evidence_class="GENUINE_PUBLIC",
    )
    canonical_hash = kalshi_baseline_quote_canonical_hash(**common)
    return KalshiBaselineQuoteEvidence(
        **common,
        captured_at=decision.evaluated_at,
        run_id="test-run",
        canonical_hash=canonical_hash,
        authority="NONE",
    )


# ---------------------------------------------------------------------------
# Basic shape / empty input
# ---------------------------------------------------------------------------


def test_empty_input_produces_no_aggregates() -> None:
    result = build_counterfactual_outcome_aggregates(
        decisions=(),
        classifications=(),
        observations=(),
        assessments=(),
        strategy_or_method_id_by_projection={},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert result == ()


def test_policy_identifier_v1_remains_the_default_and_v2_is_now_additively_known() -> None:
    """Kalshi Decision-Linked Baseline Quote Evidence v1: ``COUNTERFACTUAL_
    OUTCOME_POLICY_V2`` sits beside the frozen V1 -- exactly the same
    additive-versioning idiom already established for
    ``REASON_CLASSIFIER_V1``/``..._V2`` and ``FOLLOWUP_POLICY_V1``/``..._V2``.
    V1 remains every existing default (see the many other tests in this
    file that never pass ``outcome_policy_version`` explicitly and still
    pass unmodified)."""

    assert KNOWN_COUNTERFACTUAL_OUTCOME_POLICY_VERSIONS == (
        COUNTERFACTUAL_OUTCOME_POLICY_V1,
        COUNTERFACTUAL_OUTCOME_POLICY_V2,
        COUNTERFACTUAL_OUTCOME_POLICY_V3,
    )


def test_unknown_outcome_policy_version_raises() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(1)
    with pytest.raises(CounterfactualOutcomeError):
        build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=projections,
            run_id=_RUN_ID,
            generated_at=_NOW,
            outcome_policy_version="COUNTERFACTUAL_OUTCOME_POLICY_V99",
        )


def test_unknown_classifier_version_raises() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(1)
    with pytest.raises(CounterfactualOutcomeError):
        build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=projections,
            run_id=_RUN_ID,
            generated_at=_NOW,
            classifier_version="REASON_CLASSIFIER_V99",
        )


def test_unknown_evidence_quality_policy_version_raises() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(1)
    with pytest.raises(CounterfactualOutcomeError):
        build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=projections,
            run_id=_RUN_ID,
            generated_at=_NOW,
            evidence_quality_policy_version="FOLLOWUP_EVIDENCE_QUALITY_POLICY_V99",
        )


def test_empty_run_id_raises() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(1)
    with pytest.raises(CounterfactualOutcomeError):
        build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=projections,
            run_id="",
            generated_at=_NOW,
        )


def test_naive_generated_at_raises() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(1)
    with pytest.raises(CounterfactualOutcomeError):
        build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=projections,
            run_id=_RUN_ID,
            generated_at=datetime(2026, 8, 19),
        )


def test_missing_projection_mapping_raises() -> None:
    decisions, classifications, observations, assessments, _ = _cohort_of(1)
    with pytest.raises(CounterfactualOutcomeError):
        build_counterfactual_outcome_aggregates(
            decisions=decisions,
            classifications=classifications,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection={},
            run_id=_RUN_ID,
            generated_at=_NOW,
        )


def test_unclassified_decision_contributes_to_no_cohort() -> None:
    decisions = (_decision(decision_id="d1"),)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=(),
        observations=(_observation(decision_id="d1"),),
        assessments=(),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert result == ()


def test_decision_with_empty_reason_families_contributes_to_no_cohort() -> None:
    decisions = (_decision(decision_id="d1"),)
    classifications = (_classification(decision_id="d1", reason_families=()),)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=(_observation(decision_id="d1"),),
        assessments=(),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert result == ()


# ---------------------------------------------------------------------------
# Evidence-quality eligibility gate is actually used
# ---------------------------------------------------------------------------


def test_evidence_ineligible_observation_excluded_from_economics() -> None:
    decision = _decision(decision_id="d1")
    classification = _classification(decision_id="d1", reason_families=(ReasonFamily.ECONOMICS,))
    observation = _observation(decision_id="d1", subsequent_return=Decimal("0.10"))
    assessment = _assessment(
        observation=observation,
        verdict=FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE,
        evidence_defect_type=FollowUpEvidenceDefectType.TARGET_EVIDENCE_INELIGIBLE,
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(classification,),
        observations=(observation,),
        assessments=(assessment,),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 1
    aggregate = result[0]
    assert aggregate.total_observations_considered == 1
    assert aggregate.mature_observation_count == 1
    assert aggregate.eligible_observation_count == 0
    assert aggregate.evidence_ineligible_observation_count == 1
    assert aggregate.mean_subsequent_return is None
    assert aggregate.contributing_decision_ids == ()
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )


def test_unassessed_observation_excluded_and_tracked_separately() -> None:
    decision = _decision(decision_id="d1")
    classification = _classification(decision_id="d1", reason_families=(ReasonFamily.ECONOMICS,))
    observation = _observation(decision_id="d1", subsequent_return=Decimal("0.10"))
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(classification,),
        observations=(observation,),
        assessments=(),  # deliberately no assessment at all
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.mature_observation_count == 1
    assert aggregate.eligible_observation_count == 0
    assert aggregate.evidence_ineligible_observation_count == 0
    assert aggregate.unassessed_observation_count == 1
    assert aggregate.mean_subsequent_return is None


def test_not_yet_mature_decision_counted_as_unavailable_not_zero() -> None:
    # Two decisions structurally belong to the same cohort; only one has a
    # retained observation for this exact (horizon, baseline, policy)
    # triple -- the other must show up as "unavailable", never silently
    # become a zero-return sample.
    decision_observed = _decision(decision_id="d-observed")
    decision_pending = _decision(decision_id="d-pending")
    classifications = (
        _classification(decision_id="d-observed", reason_families=(ReasonFamily.ECONOMICS,)),
        _classification(decision_id="d-pending", reason_families=(ReasonFamily.ECONOMICS,)),
    )
    observation = _observation(decision_id="d-observed", subsequent_return=Decimal("0.02"))
    assessment = _assessment(observation=observation)
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision_observed, decision_pending),
        classifications=classifications,
        observations=(observation,),
        assessments=(assessment,),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.total_observations_considered == 2
    assert aggregate.mature_observation_count == 1
    assert aggregate.unavailable_observation_count == 1
    assert aggregate.eligible_observation_count == 1
    assert aggregate.zero_outcome_count == 0
    assert aggregate.negative_outcome_count == 0
    assert aggregate.positive_outcome_count == 1


# ---------------------------------------------------------------------------
# Minimum sample boundary: n=29 / n=30 / n=31
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected_state"),
    [
        (
            COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES - 1,
            CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE,
        ),
        (
            COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES,
            CounterfactualOutcomeQualificationState.QUALIFIED,
        ),
        (
            COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES + 1,
            CounterfactualOutcomeQualificationState.QUALIFIED,
        ),
    ],
)
def test_minimum_sample_boundary(
    count: int, expected_state: CounterfactualOutcomeQualificationState
) -> None:
    assert COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES == 30
    decisions, classifications, observations, assessments, projections = _cohort_of(
        count, returns=[Decimal("0.01")] * count
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 1
    aggregate = result[0]
    assert aggregate.eligible_observation_count == count
    assert aggregate.qualification_state is expected_state
    if expected_state is CounterfactualOutcomeQualificationState.QUALIFIED:
        assert aggregate.mean_subsequent_return is not None
    else:
        assert aggregate.mean_subsequent_return is None
        assert aggregate.median_subsequent_return is None
        assert aggregate.positive_rate is None


# ---------------------------------------------------------------------------
# Positive / negative / zero arithmetic, mean, median
# ---------------------------------------------------------------------------


def test_positive_negative_zero_arithmetic() -> None:
    returns = [
        Decimal("0.10"),
        Decimal("-0.05"),
        Decimal("0.00"),
        Decimal("0.02"),
        Decimal("-0.01"),
    ]
    decisions, classifications, observations, assessments, projections = _cohort_of(
        len(returns), returns=returns
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.positive_outcome_count == 2
    assert aggregate.negative_outcome_count == 2
    assert aggregate.zero_outcome_count == 1
    assert aggregate.positive_outcome_count + aggregate.negative_outcome_count + (
        aggregate.zero_outcome_count
    ) == len(returns)


def test_mean_and_median_exact_values() -> None:
    # n=30 (the qualification floor) so mean/median are actually reported;
    # 15x0.10 + 15x0.20, chosen so both mean and median resolve to the same
    # clean, hand-verifiable value.
    returns = [Decimal("0.10")] * 15 + [Decimal("0.20")] * 15
    decisions, classifications, observations, assessments, projections = _cohort_of(
        len(returns), returns=returns
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    assert aggregate.mean_subsequent_return == Decimal("0.15000000")
    # Even count -> average of the two middle values (0.10 + 0.20) / 2
    assert aggregate.median_subsequent_return == Decimal("0.15000000")
    assert aggregate.positive_rate == Decimal("1")


def test_positive_rate_repeating_decimal_is_quantized_not_raised() -> None:
    """Regression test for the live ``ValueValidationError`` that fired on
    the first 2026-08-25 ``COUNTERFACTUAL_OUTCOME_REFRESH`` cycle. At
    exactly the n=30 qualification floor (``COUNTERFACTUAL_OUTCOME_
    MINIMUM_SAMPLES``), a 10-of-30 positive-outcome split reduces to the
    repeating decimal 1/3 -- Python's default ``Decimal`` context renders
    that ratio to 28 significant digits, ten more than ``domain.values.
    MAX_PRECISION`` (18) permits, so the *unquantized* division raised
    ``ValueValidationError`` the moment ``canonical_hash`` tried to
    canonicalize it -- before the aggregate was even returned to the task
    executor. This is not a contrived count: 30 = 2x3x5, so at this exact
    floor *any* ``positive_count`` that is not a multiple of 3 hits this
    same shape -- the common case, not an edge case, and very plausibly the
    literal first cohort to ever reach n=30 in production (Counterfactual
    Follow-Up Backlog Throughput v1 had only just unblocked same-day
    follow-up volume). ``domain.securities_structure_outcome`` -- this
    module's sibling, built one milestone later -- already quantizes this
    exact division for this exact documented reason; this module's own
    computation was the one instance never brought in line with that
    established convention."""

    # Proof this is the real overflow shape, independent of this module:
    # the raw ratio genuinely violates domain.values' own canonical
    # precision policy, the exact mechanism the fix must satisfy.
    with pytest.raises(ValueValidationError):
        canonical_decimal(Decimal(10) / Decimal(30))

    returns = [Decimal("0.01")] * 10 + [Decimal("-0.01")] * 20
    decisions, classifications, observations, assessments, projections = _cohort_of(
        len(returns), returns=returns
    )
    build_kwargs = dict(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    # Pre-fix, this call itself raises ValueValidationError -- the exact
    # production failure -- deep inside canonical_hash computation. Two
    # independent runs over byte-identical inputs additionally prove the
    # fixed computation stays deterministic, per this module's own
    # docstring guarantee.
    first = build_counterfactual_outcome_aggregates(**build_kwargs)
    second = build_counterfactual_outcome_aggregates(**build_kwargs)
    assert len(first) == 1
    aggregate = first[0]
    assert aggregate.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    assert aggregate.positive_outcome_count == 10
    assert aggregate.negative_outcome_count == 20
    assert aggregate.positive_rate == Decimal("0.33333333")
    assert second[0].canonical_hash == aggregate.canonical_hash
    recomputed_hash = counterfactual_outcome_aggregate_canonical_hash(
        aggregate_id=aggregate.aggregate_id,
        cohort_id=aggregate.cohort_id,
        outcome_policy_version=aggregate.outcome_policy_version,
        run_id=aggregate.run_id,
        venue=aggregate.venue.value,
        strategy_or_method_id=aggregate.strategy_or_method_id,
        reason_family=aggregate.reason_family.value,
        horizon_kind=aggregate.horizon_kind.value,
        baseline_kind=aggregate.baseline_kind.value,
        followup_policy_version=aggregate.followup_policy_version,
        classifier_version=aggregate.classifier_version,
        evidence_quality_policy_version=aggregate.evidence_quality_policy_version,
        total_observations_considered=aggregate.total_observations_considered,
        mature_observation_count=aggregate.mature_observation_count,
        eligible_observation_count=aggregate.eligible_observation_count,
        evidence_ineligible_observation_count=aggregate.evidence_ineligible_observation_count,
        unassessed_observation_count=aggregate.unassessed_observation_count,
        unavailable_observation_count=aggregate.unavailable_observation_count,
        positive_outcome_count=aggregate.positive_outcome_count,
        negative_outcome_count=aggregate.negative_outcome_count,
        zero_outcome_count=aggregate.zero_outcome_count,
        mean_subsequent_return=aggregate.mean_subsequent_return,
        median_subsequent_return=aggregate.median_subsequent_return,
        positive_rate=aggregate.positive_rate,
        qualification_state=aggregate.qualification_state.value,
        contributing_decision_ids=aggregate.contributing_decision_ids,
        explanation=aggregate.explanation,
    )
    assert recomputed_hash == aggregate.canonical_hash


def test_median_odd_count() -> None:
    # n=31 (odd, above the floor): 15x0.10, one 0.50 in the middle, 15x0.90.
    returns = [Decimal("0.10")] * 15 + [Decimal("0.50")] + [Decimal("0.90")] * 15
    decisions, classifications, observations, assessments, projections = _cohort_of(
        len(returns), returns=returns
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert result[0].qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    assert result[0].median_subsequent_return == Decimal("0.50000000")
    assert result[0].mean_subsequent_return == Decimal("0.50000000")


# ---------------------------------------------------------------------------
# Deterministic hashes / replay idempotency
# ---------------------------------------------------------------------------


def test_identical_inputs_produce_byte_identical_aggregates() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(30)
    first = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    second = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert first == second
    assert first[0].aggregate_id == second[0].aggregate_id
    assert first[0].canonical_hash == second[0].canonical_hash


def test_replay_at_a_different_wall_clock_instant_is_still_byte_identical() -> None:
    # A genuine crash-resume replay under the SAME run_id but a DIFFERENT
    # ``generated_at`` (wall clock) must reproduce the identical hash --
    # ``generated_at`` is deliberately excluded from the hash formula.
    decisions, classifications, observations, assessments, projections = _cohort_of(30)
    first = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    later = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=datetime(2026, 8, 19, 0, 5, tzinfo=UTC),
    )
    assert first[0].aggregate_id == later[0].aggregate_id
    assert first[0].canonical_hash == later[0].canonical_hash
    assert first[0].generated_at != later[0].generated_at


def test_different_run_id_produces_a_new_sibling_row_not_a_mutation() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(30)
    day_one = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id="cycle-day-1",
        generated_at=_NOW,
    )
    day_two = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id="cycle-day-2",
        generated_at=_NOW,
    )
    assert day_one[0].aggregate_id != day_two[0].aggregate_id
    assert day_one[0].cohort_id == day_two[0].cohort_id
    assert day_one[0].canonical_hash != day_two[0].canonical_hash


def test_canonical_hash_helper_matches_builder_output() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(30)
    aggregate = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )[0]
    recomputed = counterfactual_outcome_aggregate_canonical_hash(
        aggregate_id=aggregate.aggregate_id,
        cohort_id=aggregate.cohort_id,
        outcome_policy_version=aggregate.outcome_policy_version,
        run_id=aggregate.run_id,
        venue=aggregate.venue.value,
        strategy_or_method_id=aggregate.strategy_or_method_id,
        reason_family=aggregate.reason_family.value,
        horizon_kind=aggregate.horizon_kind.value,
        baseline_kind=aggregate.baseline_kind.value,
        followup_policy_version=aggregate.followup_policy_version,
        classifier_version=aggregate.classifier_version,
        evidence_quality_policy_version=aggregate.evidence_quality_policy_version,
        total_observations_considered=aggregate.total_observations_considered,
        mature_observation_count=aggregate.mature_observation_count,
        eligible_observation_count=aggregate.eligible_observation_count,
        evidence_ineligible_observation_count=aggregate.evidence_ineligible_observation_count,
        unassessed_observation_count=aggregate.unassessed_observation_count,
        unavailable_observation_count=aggregate.unavailable_observation_count,
        positive_outcome_count=aggregate.positive_outcome_count,
        negative_outcome_count=aggregate.negative_outcome_count,
        zero_outcome_count=aggregate.zero_outcome_count,
        mean_subsequent_return=aggregate.mean_subsequent_return,
        median_subsequent_return=aggregate.median_subsequent_return,
        positive_rate=aggregate.positive_rate,
        qualification_state=aggregate.qualification_state.value,
        contributing_decision_ids=aggregate.contributing_decision_ids,
        explanation=aggregate.explanation,
    )
    assert recomputed == aggregate.canonical_hash


def test_cohort_id_is_a_pure_function_of_structural_key_only() -> None:
    first = counterfactual_outcome_cohort_id(
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        venue="CRYPTO",
        strategy_or_method_id=_METHOD_ID,
        reason_family="ECONOMICS",
        horizon_kind="SHORT_HORIZON",
        baseline_kind="DECISION_TIME",
        followup_policy_version=FOLLOWUP_POLICY_V2,
        classifier_version=REASON_CLASSIFIER_V2,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    second = counterfactual_outcome_cohort_id(
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        venue="CRYPTO",
        strategy_or_method_id=_METHOD_ID,
        reason_family="ECONOMICS",
        horizon_kind="SHORT_HORIZON",
        baseline_kind="DECISION_TIME",
        followup_policy_version=FOLLOWUP_POLICY_V2,
        classifier_version=REASON_CLASSIFIER_V2,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert first == second
    assert counterfactual_outcome_aggregate_id(cohort_id=first, run_id="a") != (
        counterfactual_outcome_aggregate_id(cohort_id=first, run_id="b")
    )


# ---------------------------------------------------------------------------
# Isolation: venue, horizon, baseline, method, reason family
# ---------------------------------------------------------------------------


def test_different_venue_isolation() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(
        30, venue=SharedVenue.CRYPTO, decision_id_prefix="crypto-decision"
    )
    kalshi_decisions, kalshi_classifications, kalshi_observations, kalshi_assessments, _ = (
        _cohort_of(30, venue=SharedVenue.KALSHI, decision_id_prefix="kalshi-decision")
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions + kalshi_decisions,
        classifications=classifications + kalshi_classifications,
        observations=observations + kalshi_observations,
        assessments=assessments + kalshi_assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    crypto_aggregate = _only(result, venue=SharedVenue.CRYPTO)
    kalshi_aggregate = _only(result, venue=SharedVenue.KALSHI)
    assert crypto_aggregate.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    assert crypto_aggregate.mean_subsequent_return is not None
    assert (
        kalshi_aggregate.qualification_state
        is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
    )
    assert kalshi_aggregate.mean_subsequent_return is None
    assert kalshi_aggregate.median_subsequent_return is None
    assert kalshi_aggregate.positive_rate is None
    # KALSHI evidence-quality/maturity counts remain honestly retained even
    # though no economic reduction exists.
    assert kalshi_aggregate.eligible_observation_count == 30
    assert kalshi_aggregate.total_observations_considered == 30


def test_horizon_isolation() -> None:
    # n=1 per cohort (below the qualification floor) is deliberate here --
    # this test is about isolation, not sample-floor gating, so it asserts
    # on the raw positive/negative tallies (always retained regardless of
    # qualification state) rather than the gated mean.
    decision = _decision(decision_id="d1")
    classification = _classification(decision_id="d1", reason_families=(ReasonFamily.ECONOMICS,))
    short = _observation(
        decision_id="d1",
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        subsequent_return=Decimal("0.01"),
    )
    strategy = _observation(
        decision_id="d1",
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        subsequent_return=Decimal("-0.09"),
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(classification,),
        observations=(short, strategy),
        assessments=(_assessment(observation=short), _assessment(observation=strategy)),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    short_aggregate = _only(result, horizon_kind=FollowUpHorizonKind.SHORT_HORIZON)
    strategy_aggregate = _only(result, horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON)
    assert short_aggregate.eligible_observation_count == 1
    assert strategy_aggregate.eligible_observation_count == 1
    assert short_aggregate.positive_outcome_count == 1
    assert short_aggregate.negative_outcome_count == 0
    assert strategy_aggregate.positive_outcome_count == 0
    assert strategy_aggregate.negative_outcome_count == 1


def test_baseline_kind_isolation_origin_evidence_vs_decision_time() -> None:
    # The audit's own named risk: the same decision/horizon can retain both
    # an ORIGIN_EVIDENCE and a DECISION_TIME row -- these must never pool.
    decision = _decision(decision_id="d1")
    classification = _classification(decision_id="d1", reason_families=(ReasonFamily.ECONOMICS,))
    origin = _observation(
        decision_id="d1",
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
        subsequent_return=Decimal("0.30"),
    )
    decision_time = _observation(
        decision_id="d1",
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        subsequent_return=Decimal("-0.10"),
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(classification,),
        observations=(origin, decision_time),
        assessments=(_assessment(observation=origin), _assessment(observation=decision_time)),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    origin_aggregate = _only(result, baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE)
    decision_time_aggregate = _only(result, baseline_kind=FollowUpBaselineKind.DECISION_TIME)
    assert origin_aggregate.eligible_observation_count == 1
    assert decision_time_aggregate.eligible_observation_count == 1
    assert origin_aggregate.positive_outcome_count == 1
    assert origin_aggregate.negative_outcome_count == 0
    assert decision_time_aggregate.positive_outcome_count == 0
    assert decision_time_aggregate.negative_outcome_count == 1
    # Neither cohort's count reflects the other's contribution -- proof they
    # were never pooled into one n=2 sample.
    assert origin_aggregate.total_observations_considered == 1
    assert decision_time_aggregate.total_observations_considered == 1


def test_followup_policy_version_isolation() -> None:
    # Discovered during this milestone: V1 and V2 rows can coexist for the
    # identical (venue, horizon_kind, baseline_kind) -- V1's DECISION_TIME
    # baseline leaked hindsight; V2's does not. They must not pool either.
    decision = _decision(decision_id="d1")
    classification = _classification(decision_id="d1", reason_families=(ReasonFamily.ECONOMICS,))
    v1 = _observation(
        decision_id="d1", policy_version=FOLLOWUP_POLICY_V1, subsequent_return=Decimal("0.50")
    )
    v2 = _observation(
        decision_id="d1", policy_version=FOLLOWUP_POLICY_V2, subsequent_return=Decimal("-0.20")
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(classification,),
        observations=(v1, v2),
        assessments=(_assessment(observation=v1), _assessment(observation=v2)),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    v1_aggregate = _only(result, followup_policy_version=FOLLOWUP_POLICY_V1)
    v2_aggregate = _only(result, followup_policy_version=FOLLOWUP_POLICY_V2)
    assert v1_aggregate.positive_outcome_count == 1
    assert v1_aggregate.negative_outcome_count == 0
    assert v2_aggregate.positive_outcome_count == 0
    assert v2_aggregate.negative_outcome_count == 1


def test_strategy_or_method_id_isolation() -> None:
    decisions_a, classifications_a, observations_a, assessments_a, _ = _cohort_of(
        30,
        projection_id="projection-a",
        strategy_or_method_id="METHOD_A",
        decision_id_prefix="method-a-decision",
        returns=[Decimal("0.05")] * 30,
    )
    decisions_b, classifications_b, observations_b, assessments_b, _ = _cohort_of(
        30,
        projection_id="projection-b",
        strategy_or_method_id="METHOD_B",
        decision_id_prefix="method-b-decision",
        returns=[Decimal("-0.05")] * 30,
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions_a + decisions_b,
        classifications=classifications_a + classifications_b,
        observations=observations_a + observations_b,
        assessments=assessments_a + assessments_b,
        strategy_or_method_id_by_projection={
            "projection-a": "METHOD_A",
            "projection-b": "METHOD_B",
        },
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    method_ids = {item.strategy_or_method_id for item in result}
    assert method_ids == {"METHOD_A", "METHOD_B"}
    method_a_aggregate = _only(result, strategy_or_method_id="METHOD_A")
    method_b_aggregate = _only(result, strategy_or_method_id="METHOD_B")
    assert method_a_aggregate.eligible_observation_count == 30
    assert method_b_aggregate.eligible_observation_count == 30
    assert method_a_aggregate.mean_subsequent_return == Decimal("0.05000000")
    assert method_b_aggregate.mean_subsequent_return == Decimal("-0.05000000")


def test_reason_family_isolation_and_fan_out() -> None:
    # A decision classified into TWO reason families legitimately
    # contributes to BOTH cohorts (fan-out), never double-counted within
    # either one.
    decision = _decision(decision_id="d1")
    classification = _classification(
        decision_id="d1", reason_families=(ReasonFamily.ECONOMICS, ReasonFamily.LIQUIDITY)
    )
    observation = _observation(decision_id="d1", subsequent_return=Decimal("0.04"))
    result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(classification,),
        observations=(observation,),
        assessments=(_assessment(observation=observation),),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    economics_aggregate = _only(result, reason_family=ReasonFamily.ECONOMICS)
    liquidity_aggregate = _only(result, reason_family=ReasonFamily.LIQUIDITY)
    assert economics_aggregate.contributing_decision_ids == ("d1",)
    assert liquidity_aggregate.contributing_decision_ids == ("d1",)
    # Neither cohort's own count was inflated by the fan-out.
    assert economics_aggregate.eligible_observation_count == 1
    assert liquidity_aggregate.eligible_observation_count == 1


# ---------------------------------------------------------------------------
# Classifier-version handling
# ---------------------------------------------------------------------------


def test_classifier_version_scoping() -> None:
    decision = _decision(decision_id="d1")
    v1_classification = _classification(
        decision_id="d1",
        reason_families=(ReasonFamily.OTHER_EXPLICIT,),
        classifier_version=REASON_CLASSIFIER_V1,
    )
    observation = _observation(decision_id="d1", subsequent_return=Decimal("0.01"))
    assessment = _assessment(observation=observation)

    default_result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(v1_classification,),
        observations=(observation,),
        assessments=(assessment,),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
        # classifier_version defaults to CURRENT_CLASSIFIER_VERSION (V2)
    )
    assert default_result == ()  # only a V1 classification exists

    v1_result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(v1_classification,),
        observations=(observation,),
        assessments=(assessment,),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
        classifier_version=REASON_CLASSIFIER_V1,
    )
    assert len(v1_result) == 1
    assert v1_result[0].classifier_version == REASON_CLASSIFIER_V1


def test_both_classifier_versions_retained_produce_separate_cohorts() -> None:
    decision = _decision(decision_id="d1")
    v1_classification = _classification(
        decision_id="d1",
        reason_families=(ReasonFamily.OTHER_EXPLICIT,),
        classifier_version=REASON_CLASSIFIER_V1,
    )
    v2_classification = _classification(
        decision_id="d1",
        reason_families=(ReasonFamily.ECONOMICS,),
        classifier_version=REASON_CLASSIFIER_V2,
    )
    observation = _observation(decision_id="d1", subsequent_return=Decimal("0.01"))
    assessment = _assessment(observation=observation)
    v1_result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(v1_classification, v2_classification),
        observations=(observation,),
        assessments=(assessment,),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
        classifier_version=REASON_CLASSIFIER_V1,
    )
    v2_result = build_counterfactual_outcome_aggregates(
        decisions=(decision,),
        classifications=(v1_classification, v2_classification),
        observations=(observation,),
        assessments=(assessment,),
        strategy_or_method_id_by_projection={_PROJECTION_ID: _METHOD_ID},
        run_id=_RUN_ID,
        generated_at=_NOW,
        classifier_version=REASON_CLASSIFIER_V2,
    )
    assert v1_result[0].reason_family is ReasonFamily.OTHER_EXPLICIT
    assert v2_result[0].reason_family is ReasonFamily.ECONOMICS
    # A single classifier-version run never sums decisions across versions.
    assert v1_result[0].total_observations_considered == 1
    assert v2_result[0].total_observations_considered == 1


# ---------------------------------------------------------------------------
# Insufficient sample never produces an unsupported research conclusion
# ---------------------------------------------------------------------------


def test_insufficient_sample_never_reports_a_mean_median_or_win_rate() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(5)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.mean_subsequent_return is None
    assert aggregate.median_subsequent_return is None
    assert aggregate.positive_rate is None
    # The counts themselves are still honestly retained -- INSUFFICIENT_SAMPLE
    # is visible, not silently dropped.
    assert aggregate.eligible_observation_count == 5


def test_kalshi_venue_unavailable_even_with_many_eligible_observations() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(
        50, venue=SharedVenue.KALSHI
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.eligible_observation_count == 50
    assert (
        aggregate.qualification_state
        is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
    )
    assert aggregate.mean_subsequent_return is None
    assert aggregate.median_subsequent_return is None
    assert aggregate.positive_rate is None


# ---------------------------------------------------------------------------
# Kalshi Decision-Linked Baseline Quote Evidence v1: additive
# COUNTERFACTUAL_OUTCOME_POLICY_V2 integration
# ---------------------------------------------------------------------------


def test_kalshi_v2_binary_complement_qualifies_with_enough_baseline_quotes() -> None:
    """Once >= COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES matured, eligible,
    BinaryComplementEvaluation-sourced KALSHI decisions each retain a
    baseline quote and a clean settlement, V2 reduces every one of them and
    the cohort reaches QUALIFIED -- the first genuine closing of the gap
    V1's own docstring named as its prerequisite."""

    decisions, classifications, observations, assessments, projections = _cohort_of(
        COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES,
        venue=SharedVenue.KALSHI,
        subject_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    )
    quotes = tuple(_kalshi_baseline_quote(decision=decision) for decision in decisions)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
        kalshi_baseline_quotes=quotes,
    )
    aggregate = result[0]
    expected_return = reduce_kalshi_binary_complement_outcome(
        baseline=quotes[0], observation=observations[0]
    )
    assert expected_return is not None
    assert expected_return > 0
    assert aggregate.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    assert aggregate.eligible_observation_count == COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES
    # Every decision used the identical baseline/settlement numbers, so the
    # mean is exactly that one shared per-decision return.
    assert aggregate.mean_subsequent_return == expected_return
    assert aggregate.median_subsequent_return == expected_return
    assert aggregate.positive_rate == Decimal("1")
    assert aggregate.outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V2


def test_kalshi_v1_ignores_baseline_quotes_completely() -> None:
    """The identical decisions/observations/baseline quotes as the test
    above, but under the frozen default V1 -- proves V1 is not merely
    "usually" unaffected but structurally incapable of being changed by
    ``kalshi_baseline_quotes`` at all."""

    decisions, classifications, observations, assessments, projections = _cohort_of(
        COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES,
        venue=SharedVenue.KALSHI,
        subject_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    )
    quotes = tuple(_kalshi_baseline_quote(decision=decision) for decision in decisions)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
        # outcome_policy_version omitted -> defaults to V1, exactly like
        # every pre-existing call site in this file.
        kalshi_baseline_quotes=quotes,
    )
    aggregate = result[0]
    assert (
        aggregate.qualification_state
        is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
    )
    assert aggregate.mean_subsequent_return is None
    assert aggregate.median_subsequent_return is None
    assert aggregate.positive_rate is None
    assert aggregate.outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V1


def test_kalshi_v2_threshold_bundle_stays_unavailable_method_isolation() -> None:
    """Phase 5 scopes the reduction to ``BinaryComplementEvaluation`` only;
    a ThresholdBundleEvaluation-sourced KALSHI cohort stays
    VENUE_ECONOMIC_REDUCTION_UNAVAILABLE even under V2, even with many
    eligible observations -- proving method isolation holds at the
    aggregation layer, not only inside the pure reduction function."""

    decisions, classifications, observations, assessments, projections = _cohort_of(
        COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES,
        venue=SharedVenue.KALSHI,
        subject_source_record_type=THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
        kalshi_baseline_quotes=(),
    )
    aggregate = result[0]
    assert aggregate.eligible_observation_count == COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES
    assert (
        aggregate.qualification_state
        is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
    )
    assert aggregate.mean_subsequent_return is None


def test_kalshi_v2_binary_complement_without_captured_quotes_is_insufficient_not_unavailable() -> (
    None
):
    """A structurally reducible method (V2 + BinaryComplementEvaluation)
    with genuinely zero captured baseline quotes yet must report
    INSUFFICIENT_SAMPLE -- "not enough evidence yet, will improve" -- never
    VENUE_ECONOMIC_REDUCTION_UNAVAILABLE, which means "structurally can
    never happen". These are deliberately never conflated (module
    docstring)."""

    decisions, classifications, observations, assessments, projections = _cohort_of(
        5,
        venue=SharedVenue.KALSHI,
        subject_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    )
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
        kalshi_baseline_quotes=(),
    )
    aggregate = result[0]
    assert aggregate.eligible_observation_count == 5
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.mean_subsequent_return is None


def test_kalshi_v2_below_minimum_sample_with_quotes_is_insufficient_sample() -> None:
    decisions, classifications, observations, assessments, projections = _cohort_of(
        5,
        venue=SharedVenue.KALSHI,
        subject_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    )
    quotes = tuple(_kalshi_baseline_quote(decision=decision) for decision in decisions)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
        kalshi_baseline_quotes=quotes,
    )
    aggregate = result[0]
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.mean_subsequent_return is None


def test_kalshi_v2_unsettled_observation_does_not_contribute_a_return() -> None:
    """A baseline quote exists, but the market has not genuinely settled yet
    (``settlement_result=None``): the decision is mature and eligible, but
    contributes no return -- distinguishing "not yet resolved" from a
    computed zero."""

    decisions, classifications, observations, assessments, projections = _cohort_of(
        COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES,
        venue=SharedVenue.KALSHI,
        subject_source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        settlement_result=None,
    )
    quotes = tuple(_kalshi_baseline_quote(decision=decision) for decision in decisions)
    result = build_counterfactual_outcome_aggregates(
        decisions=decisions,
        classifications=classifications,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=projections,
        run_id=_RUN_ID,
        generated_at=_NOW,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
        kalshi_baseline_quotes=quotes,
    )
    aggregate = result[0]
    # Still structurally reducible (BinaryComplementEvaluation under V2),
    # so INSUFFICIENT_SAMPLE -- never VENUE_ECONOMIC_REDUCTION_UNAVAILABLE --
    # and still honestly eligible (evidence-quality is about the follow-up
    # observation's own retained-evidence integrity, not about whether a
    # price reduction happened to succeed).
    assert aggregate.eligible_observation_count == COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.mean_subsequent_return is None
