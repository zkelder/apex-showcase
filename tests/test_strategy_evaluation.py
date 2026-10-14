from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.counterfactual_classification import ReasonFamily
from apex.domain.counterfactual_followup import FollowUpBaselineKind, FollowUpHorizonKind
from apex.domain.counterfactual_outcome import (
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeQualificationState,
)
from apex.domain.performance_attribution import (
    PaperEvidenceClass,
    StrategyHealthAssessment,
    StrategyHealthState,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyLifecycleState,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import (
    ComparisonVerdict,
    StrategyEvaluationError,
    StrategyEvaluationKind,
    compare_strategy_evaluations,
    evaluate_external_strategy,
    evaluate_internal_strategy_from_counterfactual_outcome,
    evaluate_internal_strategy_from_health,
    promote_to_active,
    promote_to_paper_eligible,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)
METHOD_ID = "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"


def _internal_definition(method_id: str = METHOD_ID) -> StrategyDefinition:
    return StrategyDefinition.create(
        source_kind=StrategySourceKind.INTERNAL,
        display_name="Internal Relative Strength (identity wrapper)",
        venue=SharedVenue.CRYPTO,
        instrument_ids=("BTC-USD",),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
        rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
        rule_parameters=StrategyRuleParameters(referenced_internal_method_id=method_id),
        evidence_requirements=StrategyEvidenceRequirements(
            minimum_history_days=1, requires_contemporaneous_observation=False
        ),
        provenance=StrategyProvenance(
            reference_kind="INTERNAL_RESEARCH_METHOD",
            reference_description=method_id,
            origin_note="Identifies Apex's own existing internal research method for comparison.",
        ),
        created_at=NOW,
    )


def _health_assessment(
    *, method_id: str = METHOD_ID, sample: int = 40, net_return: Decimal = Decimal("0.05")
) -> StrategyHealthAssessment:
    return StrategyHealthAssessment(
        health_id="health-test-1",
        policy_id="STRATEGY_HEALTH_POLICY_V1",
        strategy_or_method_id=method_id,
        strategy_or_method_version="1",
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        window_start=NOW - timedelta(days=30),
        window_end=NOW,
        paper_trial_count=sample + 5,
        fill_count=sample,
        settled_outcome_count=sample,
        expected_net_return=Decimal("0.04"),
        realized_net_return=net_return,
        fee_variance=Decimal("0.001"),
        slippage_variance=Decimal("0.001"),
        fill_rate=Decimal("0.90"),
        reconciliation_quality=Decimal("1.0"),
        maximum_drawdown=Decimal("0.02"),
        context_classifications=(),
        warnings=(),
        failed_thresholds=(),
        state=StrategyHealthState.HEALTHY,
        operator_recommendation="none",
        attribution_ids=("attribution-1",),
        canonical_hash="b" * 64,
    )


def _counterfactual_aggregate(
    *, method_id: str = METHOD_ID, eligible: int = 40, positive: int = 22, negative: int = 18
) -> CounterfactualOutcomeAggregate:
    return CounterfactualOutcomeAggregate(
        aggregate_id="aggregate-test-1",
        cohort_id="cohort-test-1",
        outcome_policy_version="COUNTERFACTUAL_OUTCOME_POLICY_V1",
        run_id="run-test-1",
        venue=SharedVenue.CRYPTO,
        strategy_or_method_id=method_id,
        reason_family=ReasonFamily.ECONOMICS,
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
        followup_policy_version="COUNTERFACTUAL_FOLLOWUP_POLICY_V2",
        classifier_version="COUNTERFACTUAL_CLASSIFICATION_V2",
        evidence_quality_policy_version="FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1",
        generated_at=NOW,
        total_observations_considered=eligible + 5,
        mature_observation_count=eligible,
        eligible_observation_count=eligible,
        evidence_ineligible_observation_count=3,
        unassessed_observation_count=2,
        unavailable_observation_count=0,
        positive_outcome_count=positive,
        negative_outcome_count=negative,
        zero_outcome_count=eligible - positive - negative,
        mean_subsequent_return=Decimal("0.01500000"),
        median_subsequent_return=Decimal("0.01000000"),
        positive_rate=Decimal(positive) / Decimal(eligible),
        qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        contributing_decision_ids=("decision-1", "decision-2"),
        explanation="test fixture",
        canonical_hash="c" * 64,
    )


class TestEvaluateExternalStrategy:
    def test_round_trips_a_real_backtest(self) -> None:
        definition = canonical_strategy_benchmarks(created_at=NOW)[0]  # buy-and-hold
        assert definition.evidence_requirements.minimum_history_days == 1
        candles = _flat_then_rising_candles()
        backtest = simulate_strategy_backtest(
            definition,
            candles,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        evaluation = evaluate_external_strategy(definition, backtest)
        assert evaluation.strategy_id == definition.strategy_id
        assert evaluation.source_kind is StrategySourceKind.EXTERNAL
        assert evaluation.evaluation_kind is StrategyEvaluationKind.EXTERNAL_BACKTEST
        assert evaluation.net_return == backtest.net_return
        assert evaluation.total_fees == backtest.total_fees
        assert evaluation.total_slippage == backtest.total_slippage
        # Review C: total_spread must round-trip too, not just fees/slippage --
        # a genuinely computed cost component, never silently dropped by this adapter.
        assert evaluation.total_spread == backtest.total_spread
        assert evaluation.total_spread is not None and evaluation.total_spread > Decimal("0")
        assert evaluation.unavailable_fields == ()

    def test_rejects_mismatched_strategy_id(self) -> None:
        benchmarks = canonical_strategy_benchmarks(created_at=NOW)
        definition, other = benchmarks[0], benchmarks[1]
        candles = _flat_then_rising_candles()
        backtest = simulate_strategy_backtest(
            definition, candles, evidence_class=EvidenceClass.FIXTURE, evidence_provenance="x"
        )
        with pytest.raises(StrategyEvaluationError, match="strategy_id"):
            evaluate_external_strategy(other, backtest)


class TestEvaluateInternalStrategyFromHealth:
    def test_maps_available_fields_and_marks_the_rest_unavailable(self) -> None:
        definition = _internal_definition()
        assessment = _health_assessment()
        evaluation = evaluate_internal_strategy_from_health(definition, assessment)
        assert evaluation.evaluation_kind is StrategyEvaluationKind.INTERNAL_PAPER_ATTRIBUTION
        assert evaluation.net_return == assessment.realized_net_return
        assert evaluation.trade_count == assessment.fill_count
        assert evaluation.gross_return is None
        assert evaluation.win_rate is None
        assert evaluation.total_spread is None  # honestly unavailable, never inferred
        assert len(evaluation.unavailable_fields) > 0

    def test_rejects_external_definition(self) -> None:
        definition = canonical_strategy_benchmarks(created_at=NOW)[0]
        with pytest.raises(StrategyEvaluationError, match="INTERNAL"):
            evaluate_internal_strategy_from_health(definition, _health_assessment())

    def test_rejects_mismatched_method_id(self) -> None:
        definition = _internal_definition(method_id="SOME_OTHER_METHOD")
        with pytest.raises(StrategyEvaluationError, match="strategy_or_method_id"):
            evaluate_internal_strategy_from_health(definition, _health_assessment())


class TestEvaluateInternalStrategyFromCounterfactualOutcome:
    def test_maps_positive_rate_into_win_rate(self) -> None:
        definition = _internal_definition()
        aggregate = _counterfactual_aggregate()
        evaluation = evaluate_internal_strategy_from_counterfactual_outcome(definition, aggregate)
        assert evaluation.evaluation_kind is StrategyEvaluationKind.INTERNAL_COUNTERFACTUAL_COHORT
        assert evaluation.win_count == aggregate.positive_outcome_count
        assert evaluation.win_rate == aggregate.positive_rate
        assert evaluation.trade_count == 0  # never a realized trade
        assert evaluation.net_return is None
        assert evaluation.total_spread is None  # this cohort never reached execution
        assert "trade-level" in " ".join(evaluation.unavailable_fields)


class TestPromoteToPaperEligible:
    def test_succeeds_with_adequate_evidence(self) -> None:
        definition = _internal_definition()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(definition, assessment)
        promoted = promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)
        assert promoted.lifecycle_state is StrategyLifecycleState.PAPER_ELIGIBLE
        assert promoted.strategy_id == definition.strategy_id
        assert promoted.version == 2
        assert definition.lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY  # untouched

    def test_counterfactual_cohort_evidence_alone_can_never_promote(self) -> None:
        # INTERNAL_COUNTERFACTUAL_COHORT evidence never carries a net_return
        # (see evaluate_internal_strategy_from_counterfactual_outcome) --
        # confirms promotion structurally requires realized-attribution-
        # grade evidence, not just decision-direction evidence, even with a
        # large, favorable sample.
        definition = _internal_definition()
        aggregate = _counterfactual_aggregate(eligible=200, positive=150, negative=50)
        evaluation = evaluate_internal_strategy_from_counterfactual_outcome(definition, aggregate)
        assert evaluation.sample_size >= 200
        with pytest.raises(StrategyEvaluationError, match="net_return"):
            promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)

    def test_rejects_insufficient_sample_size(self) -> None:
        definition = _internal_definition()
        aggregate = _counterfactual_aggregate(eligible=5, positive=4, negative=1)
        evaluation = evaluate_internal_strategy_from_counterfactual_outcome(definition, aggregate)
        with pytest.raises(StrategyEvaluationError, match="sample_size"):
            promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)

    def test_rejects_nonpositive_net_return(self) -> None:
        definition = _internal_definition()
        assessment = _health_assessment(sample=40, net_return=Decimal("-0.01"))
        evaluation = evaluate_internal_strategy_from_health(definition, assessment)
        with pytest.raises(StrategyEvaluationError, match="non-positive"):
            promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)

    def test_rejects_mismatched_strategy(self) -> None:
        definition = _internal_definition()
        other_definition = _internal_definition(method_id="A_DIFFERENT_METHOD")
        aggregate = _counterfactual_aggregate(
            method_id="A_DIFFERENT_METHOD", eligible=40, positive=30, negative=10
        )
        evaluation = evaluate_internal_strategy_from_counterfactual_outcome(
            other_definition, aggregate
        )
        with pytest.raises(StrategyEvaluationError, match="does not belong"):
            promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)

    def test_cannot_promote_twice(self) -> None:
        definition = _internal_definition()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(definition, assessment)
        promoted = promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)
        evaluation_for_promoted = evaluate_internal_strategy_from_health(promoted, assessment)
        with pytest.raises(StrategyEvaluationError, match="lifecycle_state"):
            promote_to_paper_eligible(promoted, evaluation=evaluation_for_promoted, promoted_at=NOW)


class TestPromoteToActive:
    """Recurring Strategy PAPER Orchestration v1, Phase H Strategy Lifecycle
    Guard: the one explicit, evidence-gated PAPER_ELIGIBLE -> ACTIVE
    transition, mirroring TestPromoteToPaperEligible's own coverage shape
    one lifecycle step later."""

    def _paper_eligible(self) -> StrategyDefinition:
        definition = _internal_definition()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(definition, assessment)
        return promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)

    def test_succeeds_with_adequate_evidence(self) -> None:
        paper_eligible = self._paper_eligible()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(paper_eligible, assessment)
        active = promote_to_active(paper_eligible, evaluation=evaluation, promoted_at=NOW)
        assert active.lifecycle_state is StrategyLifecycleState.ACTIVE
        assert active.strategy_id == paper_eligible.strategy_id
        assert active.version == paper_eligible.version + 1
        assert active.parent_strategy_id == paper_eligible.strategy_id
        # untouched -- no in-place mutation
        assert paper_eligible.lifecycle_state is StrategyLifecycleState.PAPER_ELIGIBLE

    def test_rejects_starting_from_research_only(self) -> None:
        definition = _internal_definition()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(definition, assessment)
        with pytest.raises(StrategyEvaluationError, match="lifecycle_state"):
            promote_to_active(definition, evaluation=evaluation, promoted_at=NOW)

    def test_rejects_insufficient_sample_size(self) -> None:
        paper_eligible = self._paper_eligible()
        assessment = _health_assessment(sample=5, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(paper_eligible, assessment)
        with pytest.raises(StrategyEvaluationError, match="sample_size"):
            promote_to_active(paper_eligible, evaluation=evaluation, promoted_at=NOW)

    def test_rejects_nonpositive_net_return(self) -> None:
        paper_eligible = self._paper_eligible()
        assessment = _health_assessment(sample=40, net_return=Decimal("-0.01"))
        evaluation = evaluate_internal_strategy_from_health(paper_eligible, assessment)
        with pytest.raises(StrategyEvaluationError, match="non-positive"):
            promote_to_active(paper_eligible, evaluation=evaluation, promoted_at=NOW)

    def test_rejects_mismatched_strategy(self) -> None:
        paper_eligible = self._paper_eligible()
        other = _internal_definition(method_id="A_DIFFERENT_METHOD")
        assessment = _health_assessment(
            method_id="A_DIFFERENT_METHOD", sample=40, net_return=Decimal("0.05")
        )
        evaluation = evaluate_internal_strategy_from_health(other, assessment)
        with pytest.raises(StrategyEvaluationError, match="does not belong"):
            promote_to_active(paper_eligible, evaluation=evaluation, promoted_at=NOW)

    def test_cannot_promote_to_active_twice(self) -> None:
        paper_eligible = self._paper_eligible()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(paper_eligible, assessment)
        active = promote_to_active(paper_eligible, evaluation=evaluation, promoted_at=NOW)
        evaluation_for_active = evaluate_internal_strategy_from_health(active, assessment)
        with pytest.raises(StrategyEvaluationError, match="lifecycle_state"):
            promote_to_active(active, evaluation=evaluation_for_active, promoted_at=NOW)

    def test_active_still_grants_no_live_authority(self) -> None:
        # Structural, not merely behavioral: no PAPER/LIVE execution path in
        # this codebase reads lifecycle_state at all (Phase A archaeology
        # confirmed zero references to StrategyLifecycleState.ACTIVE outside
        # its own enum declaration prior to this milestone).
        paper_eligible = self._paper_eligible()
        assessment = _health_assessment(sample=40, net_return=Decimal("0.05"))
        evaluation = evaluate_internal_strategy_from_health(paper_eligible, assessment)
        active = promote_to_active(paper_eligible, evaluation=evaluation, promoted_at=NOW)
        assert not hasattr(active, "live_authority")
        assert not hasattr(active, "authority")


class TestCompareStrategyEvaluations:
    def test_candidate_outperforms_benchmark(self) -> None:
        internal_definition = _internal_definition()
        strong_aggregate = _counterfactual_aggregate(eligible=40, positive=32, negative=8)
        candidate = evaluate_internal_strategy_from_counterfactual_outcome(
            internal_definition, strong_aggregate
        )

        external_definition = canonical_strategy_benchmarks(created_at=NOW)[0]
        weak_backtest = simulate_strategy_backtest(
            external_definition,
            _flat_then_falling_candles(),
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="test-capture",
        )
        benchmark = evaluate_external_strategy(external_definition, weak_backtest)

        comparison = compare_strategy_evaluations(candidate, benchmark, compared_at=NOW)
        # An INTERNAL_COUNTERFACTUAL_COHORT evaluation is decision-level
        # evidence -- it never claims a net_return figure at all (see
        # evaluate_internal_strategy_from_counterfactual_outcome), so this
        # cross-kind comparison honestly cannot compute a delta and falls
        # through to INCONCLUSIVE rather than NON_COMPARABLE: the
        # Comparability Guard (Recurring Strategy PAPER Orchestration v1
        # Phase I) gates on disjoint covered windows, not on evaluation_kind
        # identity -- comparing an internal strategy against an external
        # benchmark is this function's primary intended use, not a
        # mismatch to reject.
        assert comparison.net_return_delta is None
        assert comparison.verdict is ComparisonVerdict.INCONCLUSIVE_INSUFFICIENT_EVIDENCE

    def test_small_sample_is_inconclusive(self) -> None:
        definition = _internal_definition()
        tiny_aggregate = _counterfactual_aggregate(eligible=5, positive=4, negative=1)
        candidate = evaluate_internal_strategy_from_counterfactual_outcome(
            definition, tiny_aggregate
        )
        other_definition = _internal_definition(method_id="OTHER")
        tiny_aggregate_2 = _counterfactual_aggregate(
            method_id="OTHER", eligible=5, positive=2, negative=3
        )
        benchmark = evaluate_internal_strategy_from_counterfactual_outcome(
            other_definition, tiny_aggregate_2
        )
        comparison = compare_strategy_evaluations(candidate, benchmark, compared_at=NOW)
        assert comparison.verdict is ComparisonVerdict.INCONCLUSIVE_INSUFFICIENT_EVIDENCE
        assert any("sample size" in note for note in comparison.comparability_notes)

    def test_disjoint_covered_windows_are_non_comparable(self) -> None:
        # Comparability Guard (Recurring Strategy PAPER Orchestration v1,
        # Phase I): backtests over genuinely disjoint historical periods
        # describe different market regimes and must never be silently
        # ranked, regardless of matching evaluation_kind or sample size.
        definition = canonical_strategy_benchmarks(created_at=NOW)[0]
        early_backtest = simulate_strategy_backtest(
            definition,
            _flat_then_rising_candles(),  # 2026-01-01 .. 2026-01-05
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="early-window",
        )
        early = evaluate_external_strategy(definition, early_backtest)

        later_epoch = datetime(2027, 1, 1, tzinfo=UTC)
        later_candles = tuple(
            replace(item, bucket_start=later_epoch + timedelta(days=index))
            for index, item in enumerate(_flat_then_falling_candles())
        )
        later_backtest = simulate_strategy_backtest(
            definition,
            later_candles,
            evidence_class=EvidenceClass.FIXTURE,
            evidence_provenance="later-window",
        )
        later = evaluate_external_strategy(definition, later_backtest)

        assert early.window_end < later.window_start  # genuinely disjoint, not merely different
        comparison = compare_strategy_evaluations(early, later, compared_at=NOW)
        assert comparison.verdict is ComparisonVerdict.NON_COMPARABLE
        assert any(
            "covered windows do not overlap" in note for note in comparison.comparability_notes
        )

    def test_net_return_comparison_when_both_available(self) -> None:
        definition_a = _internal_definition()
        assessment_a = _health_assessment(sample=40, net_return=Decimal("0.08"))
        candidate = evaluate_internal_strategy_from_health(definition_a, assessment_a)

        definition_b = _internal_definition(method_id="OTHER_METHOD")
        assessment_b = _health_assessment(
            method_id="OTHER_METHOD", sample=40, net_return=Decimal("0.02")
        )
        benchmark = evaluate_internal_strategy_from_health(definition_b, assessment_b)

        comparison = compare_strategy_evaluations(candidate, benchmark, compared_at=NOW)
        assert comparison.net_return_delta == Decimal("0.06000000")
        assert comparison.verdict is ComparisonVerdict.CANDIDATE_OUTPERFORMS_BENCHMARK


def _flat_then_rising_candles() -> tuple:
    from apex.domain.crypto_research import CryptoDailyCandle

    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    prices = ["100", "105", "110", "115", "120"]
    return tuple(
        CryptoDailyCandle.create(
            capture_id="test-capture",
            attempt_id="test-attempt",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=epoch + timedelta(days=offset),
            open=price,
            high=price,
            low=price,
            close=price,
            volume="1",
            ingested_at=NOW,
            raw_response_hash="a" * 64,
        )
        for offset, price in enumerate(prices)
    )


def _flat_then_falling_candles() -> tuple:
    from apex.domain.crypto_research import CryptoDailyCandle

    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    prices = ["100", "95", "90", "85", "80"]
    return tuple(
        CryptoDailyCandle.create(
            capture_id="test-capture",
            attempt_id="test-attempt",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=epoch + timedelta(days=offset),
            open=price,
            high=price,
            low=price,
            close=price,
            volume="1",
            ingested_at=NOW,
            raw_response_hash="a" * 64,
        )
        for offset, price in enumerate(prices)
    )
