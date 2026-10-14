from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from apex.application.strategy_evaluation_history import (
    InMemoryInternalStrategyEvaluationRepository,
    InMemoryInternalStrategyPromotionRepository,
    PromoteInternalStrategy,
    RecordInternalStrategyEvaluation,
    StrategyEvaluationHistoryResearchError,
    internal_strategy_evaluation_history,
    internal_strategy_evaluation_registry,
)
from apex.application.strategy_version_provenance import (
    InMemoryStrategyVersionProvenanceRepository,
    RecordEvaluationSubject,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import StrategyLifecycleState
from apex.domain.strategy_evaluation import evaluate_external_strategy
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionRegistry,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CAPTURE_ID = "test-capture-strategy-evaluation-history-application"
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


def _bind_subject(provenance, definition, recorded) -> None:  # type: ignore[no-untyped-def]
    """Strategy Version Provenance v1: the recurring cycle binds every
    evaluation to its exact artifact right after recording it. Operator
    promotion requires that binding."""

    RecordEvaluationSubject(provenance).execute(
        definition=definition,
        registry=StrategyVersionRegistry.CATALOG_CODE,
        evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
        evaluation_id=recorded.record.evaluation_id,
        evaluation_hash=recorded.record.canonical_hash,
        evaluated_strategy_id=recorded.record.strategy_id,
        evaluated_strategy_version=recorded.record.strategy_version,
    )


class TestRecordInternalStrategyEvaluation:
    def test_genuine_new_write_then_idempotent_replay(self) -> None:
        repository = InMemoryInternalStrategyEvaluationRepository()
        service = RecordInternalStrategyEvaluation(repository)
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)

        first = service.execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert first.idempotent_replay is False

        second = service.execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(minutes=5),
        )
        assert second.idempotent_replay is True
        assert second.record.evaluation_id == first.record.evaluation_id
        assert repository.for_strategy(strategy_id=definition.strategy_id) == (first.record,)

    def test_never_holds_anything_a_paper_trial_could_be_placed_with(self) -> None:
        """Structural proof (Part 22): this service cannot call
        ``application.strategy_paper_trial.ExecuteStrategyPaperTrial`` --
        it never holds a reference to anything that service needs."""

        parameters = inspect.signature(RecordInternalStrategyEvaluation.__init__).parameters
        assert set(parameters) == {"self", "evaluations"}


class TestPromoteInternalStrategy:
    provenance: InMemoryStrategyVersionProvenanceRepository

    def setup_method(self) -> None:
        self.provenance = InMemoryStrategyVersionProvenanceRepository()

    def _qualifying_evaluation_id(
        self, evaluations: InMemoryInternalStrategyEvaluationRepository
    ) -> tuple[str, str]:
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, STRONG_PRICES)
        result = RecordInternalStrategyEvaluation(evaluations).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        _bind_subject(self.provenance, definition, result)
        return definition.strategy_id, result.record.evaluation_id

    def test_promotes_a_qualifying_evaluation_and_records_lineage(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        strategy_id, evaluation_id = self._qualifying_evaluation_id(evaluations)

        result = PromoteInternalStrategy(
            evaluations=evaluations, promotions=promotions, provenance=self.provenance
        ).execute(
            strategy_id=strategy_id,
            evaluation_id=evaluation_id,
            promoted_at=NOW,
            actor="operator:test",
        )
        assert result.idempotent_replay is False
        assert result.promotion.evaluation_id == evaluation_id
        assert result.promotion.lifecycle_state_before == StrategyLifecycleState.RESEARCH_ONLY
        assert result.promotion.lifecycle_state_after == StrategyLifecycleState.PAPER_ELIGIBLE
        assert result.promotion.actor == "operator:test"
        assert promotions.for_strategy(strategy_id=strategy_id) == (result.promotion,)

    def test_idempotent_replay_of_the_same_promotion(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        strategy_id, evaluation_id = self._qualifying_evaluation_id(evaluations)
        service = PromoteInternalStrategy(
            evaluations=evaluations, promotions=promotions, provenance=self.provenance
        )

        first = service.execute(
            strategy_id=strategy_id,
            evaluation_id=evaluation_id,
            promoted_at=NOW,
            actor="operator:test",
        )
        second = service.execute(
            strategy_id=strategy_id,
            evaluation_id=evaluation_id,
            promoted_at=NOW + timedelta(hours=1),
            actor="operator:test",
        )
        assert second.idempotent_replay is True
        assert second.promotion.promotion_id == first.promotion.promotion_id

    def test_unknown_evaluation_id_rejected(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        with pytest.raises(StrategyEvaluationHistoryResearchError):
            PromoteInternalStrategy(
                evaluations=evaluations, promotions=promotions, provenance=self.provenance
            ).execute(
                strategy_id="strategy-a",
                evaluation_id="never-retained",
                promoted_at=NOW,
                actor="operator:test",
            )

    def test_evaluation_belonging_to_a_different_strategy_rejected(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        _real_strategy_id, evaluation_id = self._qualifying_evaluation_id(evaluations)
        with pytest.raises(StrategyEvaluationHistoryResearchError):
            PromoteInternalStrategy(
                evaluations=evaluations, promotions=promotions, provenance=self.provenance
            ).execute(
                strategy_id="a-different-strategy-id",
                evaluation_id=evaluation_id,
                promoted_at=NOW,
                actor="operator:test",
            )

    def test_strategy_id_outside_the_recurring_catalog_rejected(self) -> None:
        """Part 7: never invents an additional strategy candidate -- a
        strategy_id genuinely retained on an evaluation row but no longer
        (or never) a member of the reviewed catalog fails closed rather
        than promoting something ``canonical_strategy_benchmarks`` doesn't
        recognize."""

        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        real_strategy_id, evaluation_id = self._qualifying_evaluation_id(evaluations)
        record = evaluations.get(evaluation_id=evaluation_id)
        assert record is not None
        from dataclasses import replace

        forged = replace(record, strategy_id="not-a-real-catalog-strategy-id")
        evaluations2 = InMemoryInternalStrategyEvaluationRepository()
        evaluations2.register(record=forged)
        with pytest.raises(StrategyEvaluationHistoryResearchError):
            PromoteInternalStrategy(
                evaluations=evaluations2, promotions=promotions, provenance=self.provenance
            ).execute(
                strategy_id="not-a-real-catalog-strategy-id",
                evaluation_id=evaluation_id,
                promoted_at=NOW,
                actor="operator:test",
            )
        assert real_strategy_id  # the qualifying fixture strategy_id itself is valid

    def test_weak_evaluation_is_rejected_by_the_real_promotion_gate(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        definition = _buy_and_hold_definition()
        candidate = _evaluation(definition, WEAK_PRICES)
        result = RecordInternalStrategyEvaluation(evaluations).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        _bind_subject(self.provenance, definition, result)
        with pytest.raises(StrategyEvaluationHistoryResearchError, match="non-positive"):
            PromoteInternalStrategy(
                evaluations=evaluations, promotions=promotions, provenance=self.provenance
            ).execute(
                strategy_id=definition.strategy_id,
                evaluation_id=result.record.evaluation_id,
                promoted_at=NOW,
                actor="operator:test",
            )

    def test_never_imports_anything_a_paper_trial_could_be_placed_with(self) -> None:
        """Structural proof (Part 9/22): this service cannot call
        ``application.strategy_paper_trial.ExecuteStrategyPaperTrial`` --
        it never holds a reference to anything that service needs."""

        parameters = inspect.signature(PromoteInternalStrategy.__init__).parameters
        assert set(parameters) == {"self", "evaluations", "promotions", "provenance"}


class TestReadModel:
    def test_history_reflects_current_state_vs_latest_recommendation_honestly(self) -> None:
        """Part 14: current_lifecycle_state (durable, promotion-derived) and
        latest_evaluation.qualification (what current evidence suggests)
        may legitimately differ -- proven here, not merely asserted."""

        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        definition = _buy_and_hold_definition()

        strong = _evaluation(definition, STRONG_PRICES)
        strong_recorded = RecordInternalStrategyEvaluation(evaluations).execute(
            definition=definition,
            candidate=strong,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        provenance = InMemoryStrategyVersionProvenanceRepository()
        _bind_subject(provenance, definition, strong_recorded)
        PromoteInternalStrategy(
            evaluations=evaluations, promotions=promotions, provenance=provenance
        ).execute(
            strategy_id=definition.strategy_id,
            evaluation_id=strong_recorded.record.evaluation_id,
            promoted_at=NOW,
            actor="operator:test",
        )

        # A LATER evaluation now recommends RESEARCH_ONLY (a real, weaker
        # market period) -- this module never auto-demotes (Part 8/22), so
        # current_lifecycle_state must stay PAPER_ELIGIBLE even though the
        # latest evaluation's own recommendation has moved.
        weak = _evaluation(definition, WEAK_PRICES)
        RecordInternalStrategyEvaluation(evaluations).execute(
            definition=definition,
            candidate=weak,
            cycle_id=CYCLE_ID,
            as_of=NOW + timedelta(days=1),
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(days=1),
        )

        history = internal_strategy_evaluation_history(
            evaluations=evaluations, promotions=promotions, strategy_id=definition.strategy_id
        )
        assert history["current_lifecycle_state"] == "PAPER_ELIGIBLE"
        latest_evaluation = history["latest_evaluation"]
        assert latest_evaluation is not None
        assert latest_evaluation.lifecycle_state_recommended_after == (
            StrategyLifecycleState.RESEARCH_ONLY
        )
        assert len(history["evaluations"]) == 2

    def test_no_retained_history_is_an_honest_empty_state(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        history = internal_strategy_evaluation_history(
            evaluations=evaluations, promotions=promotions, strategy_id="never-evaluated"
        )
        assert history["latest_evaluation"] is None
        assert history["latest_promotion"] is None
        assert history["current_lifecycle_state"] == "RESEARCH_ONLY"
        assert history["evaluations"] == ()

    def test_registry_covers_every_requested_strategy_id(self) -> None:
        evaluations = InMemoryInternalStrategyEvaluationRepository()
        promotions = InMemoryInternalStrategyPromotionRepository()
        strategy_ids = tuple(
            item.strategy_id for item in canonical_strategy_benchmarks(created_at=NOW)
        )
        registry = internal_strategy_evaluation_registry(
            evaluations=evaluations, promotions=promotions, strategy_ids=strategy_ids
        )
        assert set(registry) == set(strategy_ids)
        assert all(
            entry["current_lifecycle_state"] == "RESEARCH_ONLY" for entry in registry.values()
        )
