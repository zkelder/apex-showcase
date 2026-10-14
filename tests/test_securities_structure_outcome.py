"""Tests for Securities Structure Forward Performance v1
(``domain.securities_structure_outcome``)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

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
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities_structure_outcome import (
    KNOWN_SECURITIES_STRUCTURE_OUTCOME_POLICY_VERSIONS,
    SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES,
    SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
    SecuritiesStructureOutcomeError,
    SecuritiesStructureOutcomeQualificationState,
    build_securities_structure_outcome_aggregates,
    securities_structure_outcome_aggregate_canonical_hash,
    securities_structure_outcome_aggregate_id,
    securities_structure_outcome_cohort_id,
)
from apex.domain.shared_opportunity import SharedVenue

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_PROJECTION_ID = "projection-1"
_METHOD_ID = "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"
_STRUCTURE_TYPE = "CALL_CREDIT_VERTICAL"
_RUN_ID = "cycle-2026-08-24-securities-structure-outcome"

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _decision(
    *,
    decision_id: str,
    venue: SharedVenue = SharedVenue.SECURITIES,
    origin: DecisionOrigin = DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
    projection_id: str = _PROJECTION_ID,
    disposition: DecisionDisposition = DecisionDisposition.ADVANCED,
    subject_source_record_type: str = "OptionStructureCandidate",
) -> CounterfactualDecision:
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=origin,
        origin_decision_id=f"{decision_id}-origin",
        origin_canonical_hash="a" * 64,
        origin_state="ELIGIBLE" if disposition is DecisionDisposition.ADVANCED else "REJECTED",
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
        raw_reason_codes=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
        explanation="test decision",
        canonical_hash="b" * 64,
        authority="NONE",
    )


def _observation(
    *,
    decision_id: str,
    horizon_kind: FollowUpHorizonKind = FollowUpHorizonKind.SHORT_HORIZON,
    baseline_kind: FollowUpBaselineKind = FollowUpBaselineKind.DECISION_TIME,
    policy_version: str = FOLLOWUP_POLICY_V2,
    subsequent_return: Decimal | None = Decimal("0.05"),
    venue: SharedVenue = SharedVenue.SECURITIES,
) -> DecisionFollowUpObservation:
    observation_id = (
        f"{decision_id}-{horizon_kind.value}-{baseline_kind.value}-{policy_version}-obs"
    )
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        venue=venue,
        disposition=DecisionDisposition.ADVANCED,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        horizon_days=1,
        decision_evaluated_at=_NOW,
        baseline_time=_NOW,
        target_time=_NOW,
        observation_type=FollowUpObservationType.SECURITIES_STRUCTURE_TERMINAL_PAYOFF,
        subject_instrument_id="XLK",
        subject_product_or_market_id="structure-candidate-1",
        provenance=EvidenceClass.GENUINE_PUBLIC,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version=None,
        observed_at=_NOW,
        source_lineage=("batch-1",),
        data_limitations=(
            "SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT",
        ),
        reference_price_at_baseline=Decimal("187.5"),
        reference_price_at_target=Decimal("185"),
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
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
        reason_code="SECURITIES_OBSERVATION_ALWAYS_GENUINE_PUBLIC_ALPACA_CAPTURE",
        evidence_defect_type=evidence_defect_type,
        assessed_at=_NOW,
        canonical_hash=f"{observation.observation_id}-assessment-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


def _cohort_of(
    n: int,
    *,
    returns: list[Decimal] | None = None,
    structure_type: str = _STRUCTURE_TYPE,
    strategy_or_method_id: str = _METHOD_ID,
    projection_id_prefix: str = "projection",
    decision_id_prefix: str = "decision",
    all_eligible: bool = True,
) -> tuple[
    tuple[CounterfactualDecision, ...],
    tuple[DecisionFollowUpObservation, ...],
    tuple[FollowUpEvidenceQualityAssessment, ...],
    dict[str, str],
    dict[str, str],
]:
    values = returns if returns is not None else [Decimal("0.01")] * n
    decisions: list[CounterfactualDecision] = []
    observations: list[DecisionFollowUpObservation] = []
    assessments: list[FollowUpEvidenceQualityAssessment] = []
    strategy_by_projection: dict[str, str] = {}
    structure_by_projection: dict[str, str] = {}
    for index in range(n):
        decision_id = f"{decision_id_prefix}-{index}"
        projection_id = f"{projection_id_prefix}-{index}"
        decisions.append(_decision(decision_id=decision_id, projection_id=projection_id))
        strategy_by_projection[projection_id] = strategy_or_method_id
        structure_by_projection[projection_id] = structure_type
        observation = _observation(decision_id=decision_id, subsequent_return=values[index])
        observations.append(observation)
        if all_eligible:
            assessments.append(_assessment(observation=observation))
    return (
        tuple(decisions),
        tuple(observations),
        tuple(assessments),
        strategy_by_projection,
        structure_by_projection,
    )


def _only(aggregates: tuple, **kwargs: object):
    matches = [
        item
        for item in aggregates
        if all(getattr(item, key) == value for key, value in kwargs.items())
    ]
    assert len(matches) == 1, f"expected exactly one match for {kwargs}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Core aggregation behavior
# ---------------------------------------------------------------------------


def test_below_minimum_sample_is_insufficient_sample() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(5)
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.eligible_observation_count == 5
    assert (
        aggregate.qualification_state
        is SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.mean_subsequent_return is None
    assert aggregate.median_subsequent_return is None
    assert aggregate.positive_rate is None


def test_qualifies_at_minimum_sample_threshold() -> None:
    n = SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(
        n, returns=[Decimal("0.10")] * n
    )
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.qualification_state is SecuritiesStructureOutcomeQualificationState.QUALIFIED
    assert aggregate.eligible_observation_count == n
    assert aggregate.mean_subsequent_return == Decimal("0.10000000")
    assert aggregate.median_subsequent_return == Decimal("0.10000000")
    assert aggregate.positive_rate == Decimal("1")


def test_mixed_returns_compute_correct_positive_negative_zero_counts() -> None:
    n = SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES
    returns = [Decimal("0.10")] * 10 + [Decimal("-0.05")] * 15 + [Decimal("0")] * (n - 25)
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(
        n, returns=returns
    )
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.positive_outcome_count == 10
    assert aggregate.negative_outcome_count == 15
    assert aggregate.zero_outcome_count == n - 25


# ---------------------------------------------------------------------------
# Filtering: only ADVANCED, SECURITIES, SHARED_OPPORTUNITY_ELIGIBILITY-origin
# decisions ever contribute -- the entire reason this module exists as a
# complementary mechanism to ``domain.counterfactual_outcome``, not an
# overlapping one.
# ---------------------------------------------------------------------------


def test_rejected_securities_decision_never_contributes() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(5)
    rejected = _decision(
        decision_id="rejected-1",
        projection_id="projection-rejected",
        disposition=DecisionDisposition.NOT_ADVANCED,
    )
    strategy_map["projection-rejected"] = _METHOD_ID
    structure_map["projection-rejected"] = _STRUCTURE_TYPE
    result = build_securities_structure_outcome_aggregates(
        decisions=(*decisions, rejected),
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    # Still exactly 5 -- the rejected decision contributed nothing, not even
    # to ``total_decisions_considered``.
    assert aggregate.total_decisions_considered == 5
    assert "rejected-1" not in aggregate.contributing_decision_ids


def test_mandate_origin_decision_never_contributes() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(5)
    mandate_decision = _decision(
        decision_id="mandate-1",
        projection_id="projection-mandate",
        origin=DecisionOrigin.INVESTMENT_MANDATE_EVALUATION,
        disposition=DecisionDisposition.NOT_ADVANCED,
    )
    strategy_map["projection-mandate"] = _METHOD_ID
    structure_map["projection-mandate"] = _STRUCTURE_TYPE
    result = build_securities_structure_outcome_aggregates(
        decisions=(*decisions, mandate_decision),
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.total_decisions_considered == 5


def test_crypto_and_kalshi_decisions_never_contribute() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(5)
    other_venue_decisions = tuple(
        _decision(
            decision_id=f"other-{venue.value}", venue=venue, projection_id=f"proj-{venue.value}"
        )
        for venue in (SharedVenue.CRYPTO, SharedVenue.KALSHI)
    )
    for decision in other_venue_decisions:
        strategy_map[decision.subject_projection_id] = _METHOD_ID
        structure_map[decision.subject_projection_id] = _STRUCTURE_TYPE
    result = build_securities_structure_outcome_aggregates(
        decisions=(*decisions, *other_venue_decisions),
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 1
    assert result[0].total_decisions_considered == 5


def test_non_securities_observation_never_consulted() -> None:
    """A CRYPTO-venue observation sharing the same ``decision_id`` namespace
    must never leak into a SECURITIES cohort's ``observation_by_key`` --
    proves the venue filter on ``observations`` (not just ``decisions``)."""

    decisions, _observations, assessments, strategy_map, structure_map = _cohort_of(1)
    crypto_shaped = _observation(
        decision_id=decisions[0].decision_id,
        venue=SharedVenue.CRYPTO,
        subsequent_return=Decimal("9"),
    )
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=(crypto_shaped,),  # deliberately NOT the real SECURITIES observation
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert result == ()


# ---------------------------------------------------------------------------
# Partitioning discipline
# ---------------------------------------------------------------------------


def test_structure_type_partitions_separately() -> None:
    call = _cohort_of(
        3,
        structure_type="CALL_CREDIT_VERTICAL",
        decision_id_prefix="call",
        projection_id_prefix="call-proj",
    )
    call_decisions, call_observations, call_assessments, call_strategy, call_structure = call
    put = _cohort_of(
        4,
        structure_type="PUT_CREDIT_VERTICAL",
        decision_id_prefix="put",
        projection_id_prefix="put-proj",
    )
    put_decisions, put_observations, put_assessments, put_strategy, put_structure = put
    result = build_securities_structure_outcome_aggregates(
        decisions=(*call_decisions, *put_decisions),
        observations=(*call_observations, *put_observations),
        assessments=(*call_assessments, *put_assessments),
        strategy_or_method_id_by_projection={**call_strategy, **put_strategy},
        structure_type_by_projection={**call_structure, **put_structure},
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    call_aggregate = _only(result, structure_type="CALL_CREDIT_VERTICAL")
    put_aggregate = _only(result, structure_type="PUT_CREDIT_VERTICAL")
    assert call_aggregate.total_decisions_considered == 3
    assert put_aggregate.total_decisions_considered == 4
    assert call_aggregate.cohort_id != put_aggregate.cohort_id


def test_horizon_kind_partitions_separately_not_summed() -> None:
    decisions, _, _, strategy_map, structure_map = _cohort_of(1)
    decision_id = decisions[0].decision_id
    short_observation = _observation(
        decision_id=decision_id,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        subsequent_return=Decimal("0.02"),
    )
    strategy_observation = _observation(
        decision_id=decision_id,
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        subsequent_return=Decimal("0.30"),
    )
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=(short_observation, strategy_observation),
        assessments=(
            _assessment(observation=short_observation),
            _assessment(observation=strategy_observation),
        ),
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    short_aggregate = _only(result, horizon_kind=FollowUpHorizonKind.SHORT_HORIZON)
    strategy_aggregate = _only(result, horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON)
    assert short_aggregate.eligible_observation_count == 1
    assert strategy_aggregate.eligible_observation_count == 1
    assert short_aggregate.cohort_id != strategy_aggregate.cohort_id


def test_followup_policy_version_partitions_separately() -> None:
    decisions, _, _, strategy_map, structure_map = _cohort_of(1)
    decision_id = decisions[0].decision_id
    v1_observation = _observation(
        decision_id=decision_id,
        policy_version=FOLLOWUP_POLICY_V1,
        subsequent_return=Decimal("0.50"),
    )
    v2_observation = _observation(
        decision_id=decision_id,
        policy_version=FOLLOWUP_POLICY_V2,
        subsequent_return=Decimal("-0.20"),
    )
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=(v1_observation, v2_observation),
        assessments=(
            _assessment(observation=v1_observation),
            _assessment(observation=v2_observation),
        ),
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert len(result) == 2
    v1_aggregate = _only(result, followup_policy_version=FOLLOWUP_POLICY_V1)
    v2_aggregate = _only(result, followup_policy_version=FOLLOWUP_POLICY_V2)
    assert v1_aggregate.mean_subsequent_return is None  # below minimum sample, but distinct cell
    assert v2_aggregate.mean_subsequent_return is None
    assert v1_aggregate.cohort_id != v2_aggregate.cohort_id


# ---------------------------------------------------------------------------
# Evidence-quality gating (reuses the centralized, already-tested
# ``is_evidence_eligible_for_downstream_attribution`` gate -- these tests
# prove this module calls it correctly, not that the gate itself is right).
# ---------------------------------------------------------------------------


def test_unassessed_observation_counted_but_not_eligible() -> None:
    decisions, observations, _assessments, strategy_map, structure_map = _cohort_of(1)
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=(),  # deliberately no assessment retained yet
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.mature_observation_count == 1
    assert aggregate.unassessed_observation_count == 1
    assert aggregate.eligible_observation_count == 0
    assert aggregate.contributing_decision_ids == ()


def test_ineligible_evidence_excluded_from_returns() -> None:
    decisions, observations, _assessments, strategy_map, structure_map = _cohort_of(1)
    ineligible_assessment = _assessment(
        observation=observations[0],
        verdict=FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE,
        evidence_defect_type=FollowUpEvidenceDefectType.TARGET_EVIDENCE_INELIGIBLE,
    )
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=(ineligible_assessment,),
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.evidence_ineligible_observation_count == 1
    assert aggregate.eligible_observation_count == 0


def test_unmatured_decision_counted_as_unavailable() -> None:
    decisions, _observations, _assessments, strategy_map, structure_map = _cohort_of(1)
    # No observations at all -- decision exists but has not yet matured to
    # any horizon.
    result = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=(),
        assessments=(),
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    assert result == ()  # no observations at all -> zero triples -> zero cohorts, honestly


def test_some_matured_some_not_reports_unavailable_correctly() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(3)
    extra_decision = _decision(decision_id="extra-1", projection_id="extra-proj")
    strategy_map["extra-proj"] = _METHOD_ID
    structure_map["extra-proj"] = _STRUCTURE_TYPE
    result = build_securities_structure_outcome_aggregates(
        decisions=(*decisions, extra_decision),
        observations=observations,  # extra_decision has no observation
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )
    aggregate = result[0]
    assert aggregate.total_decisions_considered == 4
    assert aggregate.mature_observation_count == 3
    assert aggregate.unavailable_observation_count == 1


# ---------------------------------------------------------------------------
# Referential integrity / input validation
# ---------------------------------------------------------------------------


def test_missing_strategy_mapping_raises() -> None:
    decisions, observations, assessments, _strategy_map, structure_map = _cohort_of(1)
    with pytest.raises(SecuritiesStructureOutcomeError, match="PROJECTION_MISSING"):
        build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection={},
            structure_type_by_projection=structure_map,
            run_id=_RUN_ID,
            generated_at=_NOW,
        )


def test_missing_structure_type_mapping_raises() -> None:
    decisions, observations, assessments, strategy_map, _structure_map = _cohort_of(1)
    with pytest.raises(SecuritiesStructureOutcomeError, match="STRUCTURE_TYPE_MISSING"):
        build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=strategy_map,
            structure_type_by_projection={},
            run_id=_RUN_ID,
            generated_at=_NOW,
        )


def test_unknown_policy_version_raises() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(1)
    with pytest.raises(SecuritiesStructureOutcomeError, match="POLICY_VERSION_UNKNOWN"):
        build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=strategy_map,
            structure_type_by_projection=structure_map,
            run_id=_RUN_ID,
            generated_at=_NOW,
            outcome_policy_version="SECURITIES_STRUCTURE_OUTCOME_POLICY_V99",
        )


def test_empty_run_id_raises() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(1)
    with pytest.raises(SecuritiesStructureOutcomeError, match="RUN_ID_INVALID"):
        build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=strategy_map,
            structure_type_by_projection=structure_map,
            run_id="",
            generated_at=_NOW,
        )


def test_naive_generated_at_raises() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(1)
    with pytest.raises(SecuritiesStructureOutcomeError, match="GENERATED_AT_INVALID"):
        build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=strategy_map,
            structure_type_by_projection=structure_map,
            run_id=_RUN_ID,
            generated_at=datetime(2026, 8, 24),  # no tzinfo
        )


def test_known_policy_versions_is_exactly_v1() -> None:
    assert KNOWN_SECURITIES_STRUCTURE_OUTCOME_POLICY_VERSIONS == (
        SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
    )


# ---------------------------------------------------------------------------
# Identity / determinism / idempotent replay
# ---------------------------------------------------------------------------


def test_identical_inputs_different_generated_at_same_run_id_produce_identical_hash() -> None:
    n = SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(
        n, returns=[Decimal("0.10")] * n
    )
    first = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=_NOW,
    )[0]
    second = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id=_RUN_ID,
        generated_at=datetime(2027, 1, 1, tzinfo=UTC),  # different wall-clock stamp
    )[0]
    assert first.aggregate_id == second.aggregate_id
    assert first.canonical_hash == second.canonical_hash
    assert first.generated_at != second.generated_at  # the one field allowed to differ


def test_different_run_id_same_cohort_produces_new_aggregate_id() -> None:
    decisions, observations, assessments, strategy_map, structure_map = _cohort_of(3)
    first = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id="run-a",
        generated_at=_NOW,
    )[0]
    second = build_securities_structure_outcome_aggregates(
        decisions=decisions,
        observations=observations,
        assessments=assessments,
        strategy_or_method_id_by_projection=strategy_map,
        structure_type_by_projection=structure_map,
        run_id="run-b",
        generated_at=_NOW,
    )[0]
    assert first.cohort_id == second.cohort_id
    assert first.aggregate_id != second.aggregate_id


def test_id_and_hash_helpers_are_pure_and_reproducible() -> None:
    cohort_id = securities_structure_outcome_cohort_id(
        outcome_policy_version=SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
        strategy_or_method_id=_METHOD_ID,
        structure_type=_STRUCTURE_TYPE,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    cohort_id_again = securities_structure_outcome_cohort_id(
        outcome_policy_version=SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
        strategy_or_method_id=_METHOD_ID,
        structure_type=_STRUCTURE_TYPE,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert cohort_id == cohort_id_again
    aggregate_id = securities_structure_outcome_aggregate_id(cohort_id=cohort_id, run_id=_RUN_ID)
    assert aggregate_id != cohort_id
    common_hash_kwargs = dict(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
        run_id=_RUN_ID,
        strategy_or_method_id=_METHOD_ID,
        structure_type=_STRUCTURE_TYPE,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        total_decisions_considered=1,
        mature_observation_count=1,
        eligible_observation_count=1,
        evidence_ineligible_observation_count=0,
        unassessed_observation_count=0,
        unavailable_observation_count=0,
        positive_outcome_count=1,
        negative_outcome_count=0,
        zero_outcome_count=0,
        mean_subsequent_return=None,
        median_subsequent_return=None,
        positive_rate=None,
        qualification_state=SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE.value,
        contributing_decision_ids=("decision-0",),
        explanation="test",
    )
    hash_a = securities_structure_outcome_aggregate_canonical_hash(**common_hash_kwargs)
    hash_b = securities_structure_outcome_aggregate_canonical_hash(**common_hash_kwargs)
    assert hash_a == hash_b
    assert len(hash_a) == 64
