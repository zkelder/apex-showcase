from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.external_strategy import ExternalStrategyQualification
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import (
    canonical_strategy_benchmarks,
    simulate_strategy_backtest,
)
from apex.domain.strategy_definition import StrategyLifecycleState, next_version
from apex.domain.strategy_evaluation import evaluate_external_strategy
from apex.domain.strategy_evaluation_history import (
    INTERNAL_STRATEGY_EVALUATION_FRESHNESS_WINDOW,
    InternalStrategyEvaluationFreshness,
    InternalStrategyPromotionRecord,
    StrategyEvaluationHistoryError,
    build_internal_strategy_evaluation_record,
    build_internal_strategy_promotion_record,
    current_lifecycle_state,
    evaluate_freshness,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CAPTURE_ID = "test-capture-strategy-evaluation-history"
CYCLE_ID = "recurring-strategy-paper-plan-v1-worker-20260902"


def _candles(prices: list[str]) -> tuple[CryptoDailyCandle, ...]:
    return tuple(
        CryptoDailyCandle.create(
            capture_id=CAPTURE_ID,
            attempt_id=f"attempt-{offset}",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=_EPOCH + timedelta(days=offset),
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


def _rising_prices(count: int, *, start: int = 100, step: int = 5) -> list[str]:
    return [str(start + step * index) for index in range(count)]


def _falling_prices(count: int, *, start: int = 300, step: int = 5) -> list[str]:
    return [str(start - step * index) for index in range(count)]


def _buy_and_hold_definition():
    (buy_and_hold, _ma, _momentum, _reversion) = canonical_strategy_benchmarks(created_at=NOW)
    return buy_and_hold


def _evaluation(definition, prices: list[str]):
    candles = _candles(prices)
    backtest = simulate_strategy_backtest(
        definition, candles, evidence_class=EvidenceClass.FIXTURE, evidence_provenance=CAPTURE_ID
    )
    return evaluate_external_strategy(definition, backtest)


STRONG_PRICES = _rising_prices(35)
WEAK_PRICES = _falling_prices(35)
THIN_PRICES = _rising_prices(5)


class TestBuildInternalStrategyEvaluationRecord:
    def test_captures_the_exact_result_passed_in(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        record = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert record.candidate == candidate
        assert record.strategy_id == definition.strategy_id
        assert record.strategy_version == definition.version
        assert record.source_kind == definition.source_kind
        assert record.venue == definition.venue
        assert record.instrument_ids == definition.instrument_ids
        assert record.rule_kind == definition.rule_kind
        assert record.cycle_id == CYCLE_ID
        assert record.capture_id == CAPTURE_ID
        assert record.lifecycle_state_before == StrategyLifecycleState.RESEARCH_ONLY
        assert len(record.canonical_hash) == 64
        assert record.authority == "NONE"

    def test_strong_positive_return_qualifies_and_recommends_paper_eligible(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        assert candidate.sample_size >= 30
        assert candidate.net_return is not None and candidate.net_return > 0
        record = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert (
            record.qualification is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        assert record.lifecycle_state_recommended_after == StrategyLifecycleState.PAPER_ELIGIBLE

    def test_negative_return_stays_research_only_nonpositive_net_return(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, WEAK_PRICES)
        assert candidate.sample_size >= 30
        assert candidate.net_return is not None and candidate.net_return <= 0
        record = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert (
            record.qualification
            is ExternalStrategyQualification.RESEARCH_ONLY_NONPOSITIVE_NET_RETURN
        )
        assert record.lifecycle_state_recommended_after == StrategyLifecycleState.RESEARCH_ONLY

    def test_thin_sample_stays_research_only_insufficient_sample(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, THIN_PRICES)
        assert candidate.sample_size < 30
        record = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert (
            record.qualification is ExternalStrategyQualification.RESEARCH_ONLY_INSUFFICIENT_SAMPLE
        )
        assert record.lifecycle_state_recommended_after == StrategyLifecycleState.RESEARCH_ONLY

    def test_mismatched_strategy_id_rejected(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        other_definitions = canonical_strategy_benchmarks(created_at=NOW)
        other = next(
            item for item in other_definitions if item.strategy_id != definition.strategy_id
        )
        with pytest.raises(StrategyEvaluationHistoryError):
            build_internal_strategy_evaluation_record(
                definition=other,
                candidate=candidate,
                cycle_id=CYCLE_ID,
                as_of=NOW,
                capture_id=CAPTURE_ID,
                evaluated_at=NOW,
            )

    def test_blank_cycle_id_rejected(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        with pytest.raises(StrategyEvaluationHistoryError):
            build_internal_strategy_evaluation_record(
                definition=definition,
                candidate=candidate,
                cycle_id="   ",
                as_of=NOW,
                capture_id=CAPTURE_ID,
                evaluated_at=NOW,
            )


class TestDeterministicIdentity:
    """Part 6: replay-safe on identical inputs; distinct on any genuine change."""

    def test_identical_inputs_replay_the_same_identity(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        first = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        second = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(minutes=5),  # a later same-day tick
        )
        assert first.evaluation_id == second.evaluation_id
        assert first.canonical_hash == second.canonical_hash

    def test_different_cycle_id_alone_does_not_change_identity(self) -> None:
        """cycle_id is captured lineage, never identity (see module
        docstring) -- two dispatches of the SAME evidence under two
        different cycle_ids (a pathological/test-only scenario; production
        cycle_id is stable all UTC day) still resolve to one evaluation."""

        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        first = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        second = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id="a-different-cycle-id",
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert first.evaluation_id == second.evaluation_id

    def test_changed_as_of_produces_distinct_history(self) -> None:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        first = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        second = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW + timedelta(days=1),
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(days=1),
        )
        assert first.evaluation_id != second.evaluation_id

    def test_changed_evidence_produces_distinct_history(self) -> None:
        definition = _buy_and_hold_definition()
        strong = _evaluation(definition, STRONG_PRICES)
        weak = _evaluation(definition, WEAK_PRICES)
        first = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=strong,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        second = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=weak,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert first.evaluation_id != second.evaluation_id

    def test_changed_strategy_version_produces_distinct_history(self) -> None:
        definition = _buy_and_hold_definition()
        candidate_v1 = _evaluation(definition, STRONG_PRICES)
        first = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate_v1,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )

        definition_v2 = next_version(
            definition, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        candidate_v2 = _evaluation(definition_v2, STRONG_PRICES)
        second = build_internal_strategy_evaluation_record(
            definition=definition_v2,
            candidate=candidate_v2,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert definition_v2.version != definition.version
        assert first.evaluation_id != second.evaluation_id


class TestBuildInternalStrategyPromotionRecord:
    def test_deterministic_replay_on_identical_inputs(self) -> None:
        first = build_internal_strategy_promotion_record(
            strategy_id="strategy-a",
            strategy_version=1,
            evaluation_id="evaluation-a",
            lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
            lifecycle_state_after=StrategyLifecycleState.PAPER_ELIGIBLE,
            promoted_at=NOW,
            actor="operator:cli",
        )
        second = build_internal_strategy_promotion_record(
            strategy_id="strategy-a",
            strategy_version=1,
            evaluation_id="evaluation-a",
            lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
            lifecycle_state_after=StrategyLifecycleState.PAPER_ELIGIBLE,
            promoted_at=NOW + timedelta(hours=1),  # a later real invocation
            actor="operator:cli",
        )
        assert first.promotion_id == second.promotion_id
        assert first.canonical_hash == second.canonical_hash

    def test_different_evaluation_id_mints_a_distinct_promotion(self) -> None:
        first = build_internal_strategy_promotion_record(
            strategy_id="strategy-a",
            strategy_version=1,
            evaluation_id="evaluation-a",
            lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
            lifecycle_state_after=StrategyLifecycleState.PAPER_ELIGIBLE,
            promoted_at=NOW,
            actor="operator:cli",
        )
        second = build_internal_strategy_promotion_record(
            strategy_id="strategy-a",
            strategy_version=1,
            evaluation_id="evaluation-b",
            lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
            lifecycle_state_after=StrategyLifecycleState.PAPER_ELIGIBLE,
            promoted_at=NOW,
            actor="operator:cli",
        )
        assert first.promotion_id != second.promotion_id

    def test_blank_actor_rejected(self) -> None:
        with pytest.raises(StrategyEvaluationHistoryError):
            build_internal_strategy_promotion_record(
                strategy_id="strategy-a",
                strategy_version=1,
                evaluation_id="evaluation-a",
                lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
                lifecycle_state_after=StrategyLifecycleState.PAPER_ELIGIBLE,
                promoted_at=NOW,
                actor="  ",
            )


def _promotion(
    *, evaluation_id: str, promoted_at: datetime, lifecycle_after: StrategyLifecycleState
) -> InternalStrategyPromotionRecord:
    return build_internal_strategy_promotion_record(
        strategy_id="strategy-a",
        strategy_version=1,
        evaluation_id=evaluation_id,
        lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
        lifecycle_state_after=lifecycle_after,
        promoted_at=promoted_at,
        actor="operator:cli",
    )


class TestCurrentLifecycleState:
    """Part 14: current operational state, derived from append-only
    promotion history alone -- never a second mutable state column."""

    def test_no_promotions_stays_research_only(self) -> None:
        assert current_lifecycle_state(()) == StrategyLifecycleState.RESEARCH_ONLY

    def test_one_promotion_governs(self) -> None:
        promotion = _promotion(
            evaluation_id="evaluation-a",
            promoted_at=NOW,
            lifecycle_after=StrategyLifecycleState.PAPER_ELIGIBLE,
        )
        assert current_lifecycle_state((promotion,)) == StrategyLifecycleState.PAPER_ELIGIBLE

    def test_latest_promotion_by_promoted_at_wins_regardless_of_input_order(self) -> None:
        earlier = _promotion(
            evaluation_id="evaluation-a",
            promoted_at=NOW,
            lifecycle_after=StrategyLifecycleState.PAPER_ELIGIBLE,
        )
        later = _promotion(
            evaluation_id="evaluation-b",
            promoted_at=NOW + timedelta(days=1),
            lifecycle_after=StrategyLifecycleState.ACTIVE,
        )
        assert current_lifecycle_state((later, earlier)) == StrategyLifecycleState.ACTIVE
        assert current_lifecycle_state((earlier, later)) == StrategyLifecycleState.ACTIVE


class TestEvaluateFreshness:
    def test_no_retained_evaluation(self) -> None:
        assert (
            evaluate_freshness(None, now=NOW) is InternalStrategyEvaluationFreshness.NEVER_EVALUATED
        )

    def test_within_window_is_fresh(self) -> None:
        recent = NOW - (INTERNAL_STRATEGY_EVALUATION_FRESHNESS_WINDOW / 2)
        assert evaluate_freshness(recent, now=NOW) is InternalStrategyEvaluationFreshness.FRESH

    def test_beyond_window_is_stale(self) -> None:
        old = NOW - INTERNAL_STRATEGY_EVALUATION_FRESHNESS_WINDOW - timedelta(seconds=1)
        assert evaluate_freshness(old, now=NOW) is InternalStrategyEvaluationFreshness.STALE
