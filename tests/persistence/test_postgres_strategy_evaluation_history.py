"""PostgreSQL persistence proofs for Strategy Evaluation History & Promotion
Lineage v1: migration 0110, ``PostgresInternalStrategyEvaluationRepository``,
``PostgresInternalStrategyPromotionRepository``. Zero genuine network
requests -- every candle here is a hand-built fixture, exactly like
``tests/test_strategy_benchmark.py``/``tests/persistence/test_postgres_
external_strategy_research.py`` construct their own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.strategy_evaluation_history_postgres import (
    PostgresInternalStrategyEvaluationRepository,
    PostgresInternalStrategyPromotionRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    PostgresStrategyVersionProvenanceRepository,
)
from apex.application.strategy_evaluation_history import (
    PromoteInternalStrategy,
    RecordInternalStrategyEvaluation,
    StrategyEvaluationHistoryResearchError,
)
from apex.application.strategy_version_provenance import RecordEvaluationSubject
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.external_strategy import ExternalStrategyQualification
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import StrategyLifecycleState
from apex.domain.strategy_evaluation import evaluate_external_strategy
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionRegistry,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)
CAPTURE_ID = "test-capture"
CYCLE_ID = "recurring-strategy-paper-plan-v1-worker-20260902"


def _candles(prices: list[str]) -> tuple[CryptoDailyCandle, ...]:
    candles = []
    for offset, price in enumerate(prices):
        candles.append(
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
        )
    return tuple(candles)


def _rising_prices(count: int, *, start: int = 100, step: int = 5) -> list[str]:
    return [str(start + step * index) for index in range(count)]


def _buy_and_hold_definition():
    (buy_and_hold, _ma, _momentum, _reversion) = canonical_strategy_benchmarks(created_at=NOW)
    return buy_and_hold


def _qualifying_evaluation():
    definition = _buy_and_hold_definition()
    candles = _candles(_rising_prices(35))
    backtest = simulate_strategy_backtest(
        definition, candles, evidence_class=EvidenceClass.FIXTURE, evidence_provenance=CAPTURE_ID
    )
    return definition, evaluate_external_strategy(definition, backtest)


class TestEvaluationRepository:
    def test_register_persists_and_round_trips(self, postgres_engine: Engine) -> None:
        repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        result = RecordInternalStrategyEvaluation(repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        assert result.idempotent_replay is False

        # A fresh repository instance over the same engine -- proves nothing
        # is cached in-process.
        reloaded = PostgresInternalStrategyEvaluationRepository(postgres_engine).get(
            evaluation_id=result.record.evaluation_id
        )
        assert reloaded == result.record
        assert (
            reloaded.qualification is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        assert reloaded.lifecycle_state_recommended_after == StrategyLifecycleState.PAPER_ELIGIBLE
        assert reloaded.candidate.net_return is not None

    def test_deterministic_replay_across_process_boundaries(self, postgres_engine: Engine) -> None:
        definition, candidate = _qualifying_evaluation()
        first_repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        first = RecordInternalStrategyEvaluation(first_repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        # A fresh repository instance, later real invocation -- simulating a
        # new process / a later same-day tick.
        second_repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        second = RecordInternalStrategyEvaluation(second_repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(minutes=5),
        )
        assert first.record.evaluation_id == second.record.evaluation_id
        assert second.idempotent_replay is True
        assert len(second_repository.for_strategy(strategy_id=definition.strategy_id)) == 1

    def test_register_raises_on_genuine_identity_conflict(self, postgres_engine: Engine) -> None:
        repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        result = RecordInternalStrategyEvaluation(repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        from dataclasses import replace

        tampered = replace(result.record, canonical_hash="f" * 64)
        with pytest.raises(StrategyEvaluationHistoryResearchError):
            repository.register(record=tampered)

    def test_for_strategy_orders_most_recent_first(self, postgres_engine: Engine) -> None:
        repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        first = RecordInternalStrategyEvaluation(repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        second = RecordInternalStrategyEvaluation(repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id="recurring-strategy-paper-plan-v1-worker-20260903",
            as_of=NOW + timedelta(days=1),
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(days=1),
        )
        assert first.record.evaluation_id != second.record.evaluation_id
        rows = repository.for_strategy(strategy_id=definition.strategy_id)
        assert [item.evaluation_id for item in rows] == [
            second.record.evaluation_id,
            first.record.evaluation_id,
        ]
        latest = repository.latest_for_strategy(strategy_id=definition.strategy_id)
        assert latest is not None
        assert latest.evaluation_id == second.record.evaluation_id


class TestPromotionRepository:
    def test_promote_persists_and_round_trips_with_evaluation_lineage(
        self, postgres_engine: Engine
    ) -> None:
        evaluations = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        promotions = PostgresInternalStrategyPromotionRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        recorded = RecordInternalStrategyEvaluation(evaluations).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        provenance = PostgresStrategyVersionProvenanceRepository(postgres_engine)
        RecordEvaluationSubject(provenance).execute(
            definition=definition,
            registry=StrategyVersionRegistry.CATALOG_CODE,
            evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
            evaluation_id=recorded.record.evaluation_id,
            evaluation_hash=recorded.record.canonical_hash,
            evaluated_strategy_id=recorded.record.strategy_id,
            evaluated_strategy_version=recorded.record.strategy_version,
        )
        result = PromoteInternalStrategy(
            evaluations=evaluations, promotions=promotions, provenance=provenance
        ).execute(
            strategy_id=definition.strategy_id,
            evaluation_id=recorded.record.evaluation_id,
            promoted_at=NOW,
            actor="operator:test",
        )
        assert result.idempotent_replay is False

        reloaded = PostgresInternalStrategyPromotionRepository(postgres_engine).get(
            promotion_id=result.promotion.promotion_id
        )
        assert reloaded == result.promotion
        assert reloaded.evaluation_id == recorded.record.evaluation_id
        assert reloaded.lifecycle_state_after == StrategyLifecycleState.PAPER_ELIGIBLE

    def test_promotion_referencing_an_unknown_evaluation_id_is_rejected_by_the_fk(
        self, postgres_engine: Engine
    ) -> None:
        from apex.domain.strategy_evaluation_history import build_internal_strategy_promotion_record

        promotions = PostgresInternalStrategyPromotionRepository(postgres_engine)
        forged = build_internal_strategy_promotion_record(
            strategy_id="strategy-a",
            strategy_version=1,
            evaluation_id="never-retained-evaluation-id",
            lifecycle_state_before=StrategyLifecycleState.RESEARCH_ONLY,
            lifecycle_state_after=StrategyLifecycleState.PAPER_ELIGIBLE,
            promoted_at=NOW,
            actor="operator:test",
        )
        with pytest.raises(StrategyEvaluationHistoryResearchError):
            promotions.register(record=forged)


class TestImmutabilityAndDowngrade:
    def test_internal_strategy_evaluation_is_immutable(self, postgres_engine: Engine) -> None:
        repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        result = RecordInternalStrategyEvaluation(repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "UPDATE internal_strategy_evaluation SET authority = 'PAPER' "
                    "WHERE evaluation_id = :id"
                ),
                {"id": result.record.evaluation_id},
            )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text("DELETE FROM internal_strategy_evaluation WHERE evaluation_id = :id"),
                {"id": result.record.evaluation_id},
            )

    def test_internal_strategy_promotion_is_immutable(self, postgres_engine: Engine) -> None:
        evaluations = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        promotions = PostgresInternalStrategyPromotionRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        recorded = RecordInternalStrategyEvaluation(evaluations).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        provenance = PostgresStrategyVersionProvenanceRepository(postgres_engine)
        RecordEvaluationSubject(provenance).execute(
            definition=definition,
            registry=StrategyVersionRegistry.CATALOG_CODE,
            evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
            evaluation_id=recorded.record.evaluation_id,
            evaluation_hash=recorded.record.canonical_hash,
            evaluated_strategy_id=recorded.record.strategy_id,
            evaluated_strategy_version=recorded.record.strategy_version,
        )
        result = PromoteInternalStrategy(
            evaluations=evaluations, promotions=promotions, provenance=provenance
        ).execute(
            strategy_id=definition.strategy_id,
            evaluation_id=recorded.record.evaluation_id,
            promoted_at=NOW,
            actor="operator:test",
        )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text("DELETE FROM internal_strategy_promotion WHERE promotion_id = :id"),
                {"id": result.promotion.promotion_id},
            )

    def test_cannot_downgrade_0110_with_retained_evaluation_evidence(
        self, postgres_engine: Engine
    ) -> None:
        from alembic import command
        from alembic.config import Config

        repository = PostgresInternalStrategyEvaluationRepository(postgres_engine)
        definition, candidate = _qualifying_evaluation()
        RecordInternalStrategyEvaluation(repository).execute(
            definition=definition,
            candidate=candidate,
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        with postgres_engine.connect() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            with pytest.raises(Exception, match="cannot downgrade 0110"):
                command.downgrade(config, "0109_external_strategy_research")
            connection.rollback()
