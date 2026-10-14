"""PostgreSQL persistence proofs for External Strategy Evaluation v1:
migration 0109, ``PostgresExternalStrategyDefinitionRepository``,
``PostgresExternalStrategyEvaluationRepository``. Zero genuine network
requests -- every candle here is a hand-built fixture, exactly like
``tests/test_strategy_benchmark.py`` constructs its own."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.external_strategy_research_postgres import (
    PostgresExternalStrategyDefinitionRepository,
    PostgresExternalStrategyEvaluationRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    PostgresStrategyVersionProvenanceRepository,
)
from apex.application.external_strategy_research import (
    EvaluateExternalStrategy,
    ExternalStrategyResearchError,
    PromoteExternalStrategy,
    RegisterExternalStrategy,
    external_strategy_detail,
    external_strategy_registry_snapshot,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.external_strategy import (
    EXTERNAL_STRATEGY_SPECIFICATION_V1,
    ExternalStrategyQualification,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import BENCHMARK_INSTRUMENT

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _spec(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "specification_version": EXTERNAL_STRATEGY_SPECIFICATION_V1,
        "display_name": "Buy and Hold BTC-USD (Manual Demo)",
        "venue": "CRYPTO",
        "instrument_id": BENCHMARK_INSTRUMENT,
        "rule_kind": "BUY_AND_HOLD",
        "rule_parameters": {},
        "reference_description": "The canonical passive baseline.",
        "origin_note": "Manually registered by an operator for this test.",
    }
    base.update(overrides)
    return base


class _FakeCandleSource:
    def __init__(self, by_capture_id: dict[str, tuple[CryptoDailyCandle, ...]]) -> None:
        self._by_capture_id = by_capture_id

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        return self._by_capture_id.get(capture_id, ())


def _candles(prices: list[str]) -> tuple[CryptoDailyCandle, ...]:
    candles = []
    for offset, price in enumerate(prices):
        candles.append(
            CryptoDailyCandle.create(
                capture_id="test-capture",
                attempt_id="test-attempt",
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


class TestDefinitionRepository:
    def test_register_persists_and_round_trips(self, postgres_engine: Engine) -> None:
        repository = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        result = RegisterExternalStrategy(repository).execute(specification=_spec(), created_at=NOW)
        assert result.idempotent_replay is False

        # A fresh repository instance over the same engine, proving nothing
        # is cached in-process.
        reloaded = PostgresExternalStrategyDefinitionRepository(postgres_engine).get(
            strategy_id=result.definition.strategy_id, version=1
        )
        assert reloaded == result.definition

    def test_register_is_idempotent_on_replay(self, postgres_engine: Engine) -> None:
        repository = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        service = RegisterExternalStrategy(repository)
        first = service.execute(specification=_spec(), created_at=NOW)
        second = service.execute(specification=_spec(), created_at=NOW + timedelta(hours=1))
        assert first.idempotent_replay is False
        assert second.idempotent_replay is True
        assert len(repository.versions(strategy_id=first.definition.strategy_id)) == 1

    def test_register_raises_on_genuine_identity_conflict(self, postgres_engine: Engine) -> None:
        repository = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        service = RegisterExternalStrategy(repository)
        service.execute(specification=_spec(), created_at=NOW)
        with pytest.raises(ExternalStrategyResearchError):
            service.execute(
                specification=_spec(display_name="A materially different name"), created_at=NOW
            )

    def test_all_root_strategies_reports_latest_version_only(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(40)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=40)
        evaluation = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        PromoteExternalStrategy(
            definitions=definitions,
            evaluations=evaluations,
            provenance=PostgresStrategyVersionProvenanceRepository(postgres_engine),
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            evaluation_id=evaluation.record.evaluation_id,
            promoted_at=NOW,
        )
        roots = definitions.all_root_strategies()
        matching = [item for item in roots if item.strategy_id == definition.strategy_id]
        assert len(matching) == 1
        assert matching[0].version == 2
        assert matching[0].lifecycle_state.value == "PAPER_ELIGIBLE"


class TestEvaluationRepository:
    def test_evaluate_persists_and_round_trips(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(90)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=90)
        result = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        reloaded = PostgresExternalStrategyEvaluationRepository(postgres_engine).get(
            evaluation_id=result.record.evaluation_id
        )
        assert reloaded == result.record
        assert (
            reloaded.qualification is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        assert reloaded.candidate.net_return is not None
        assert (
            reloaded.benchmark_comparisons
        )  # at least buy-and-hold-vs-itself is skipped as duplicate identity but others compare

    def test_deterministic_replay_across_process_boundaries(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(90)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=90)
        service = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        )
        first = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        # A fresh set of repository instances -- simulating a new process.
        second_definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        second_evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        second_service = EvaluateExternalStrategy(
            definitions=second_definitions, evaluations=second_evaluations, candles=candles
        )
        second = second_service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW + timedelta(days=1),
        )
        assert first.record.evaluation_id == second.record.evaluation_id
        assert second.idempotent_replay is True
        assert len(evaluations.for_strategy(strategy_id=definition.strategy_id)) == 1

    def test_evaluation_register_raises_on_genuine_identity_conflict(
        self, postgres_engine: Engine
    ) -> None:
        # Probes the evaluation repository's own conflict path directly: a
        # tampered record sharing a real evaluation_id but a different
        # canonical_hash must be rejected, never silently overwritten.
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(90)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=90)
        result = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        from dataclasses import replace

        tampered = replace(result.record, canonical_hash="f" * 64)
        with pytest.raises(ExternalStrategyResearchError):
            evaluations.register(record=tampered)


class TestImmutabilityAndDowngrade:
    def test_external_strategy_definition_is_immutable(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "UPDATE external_strategy_definition SET authority = 'PAPER' "
                    "WHERE strategy_id = :id AND version = 1"
                ),
                {"id": definition.strategy_id},
            )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "DELETE FROM external_strategy_definition "
                    "WHERE strategy_id = :id AND version = 1"
                ),
                {"id": definition.strategy_id},
            )

    def test_external_strategy_evaluation_is_immutable(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(40)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=40)
        result = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text("DELETE FROM external_strategy_evaluation WHERE evaluation_id = :id"),
                {"id": result.record.evaluation_id},
            )

    def test_cannot_downgrade_0109_with_retained_definition_evidence(
        self, postgres_engine: Engine
    ) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        RegisterExternalStrategy(definitions).execute(specification=_spec(), created_at=NOW)
        with postgres_engine.connect() as connection:
            # The downgrade guard checks reversed(EXTERNAL_STRATEGY_RESEARCH_
            # TABLES) -- evaluation first, then definition -- so with only a
            # definition retained (no evaluation), the definition check is
            # the one that genuinely fires.
            with pytest.raises(
                Exception,
                match="cannot downgrade 0109 with retained external_strategy_definition evidence",
            ):
                command.downgrade(_config(connection), "0108_kalshi_baseline_v2")
            connection.rollback()
            # NOT necessarily the fixture-established current head once a
            # later migration (0110, Strategy Evaluation History &
            # Promotion Lineage v1) stacks on top: alembic runs each
            # revision step in ``reversed(EXTERNAL_STRATEGY_RESEARCH_
            # TABLES)``'s downgrade() as its own committed step, so
            # ``command.downgrade(..., "0108_kalshi_baseline_v2")`` from a
            # head ABOVE 0109 first genuinely, successfully downgrades
            # every step down to 0109 before ever reaching 0109's own
            # downgrade() (the one that raises here) -- only THAT step's
            # own DDL rolls back. The resting point is therefore always
            # 0109 itself, a permanent fact about this migration's own
            # guard, never "whatever head happens to be current" -- proven
            # empirically via a real 0110-on-top run, not assumed.
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                "0109_external_strategy_research"
            )

    def test_authority_is_always_none(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(40)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        assert definition.canonical_payload  # sanity: real content
        as_of = _EPOCH + timedelta(days=40)
        EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        with postgres_engine.connect() as connection:
            definition_authorities = (
                connection.execute(
                    sa.text("SELECT DISTINCT authority FROM external_strategy_definition")
                )
                .scalars()
                .all()
            )
            evaluation_authorities = (
                connection.execute(
                    sa.text("SELECT DISTINCT authority FROM external_strategy_evaluation")
                )
                .scalars()
                .all()
            )
        assert set(definition_authorities) == {"NONE"}
        assert set(evaluation_authorities) == {"NONE"}


class TestReadModelAgainstPostgres:
    def test_registry_snapshot_reflects_a_promoted_strategy(self, postgres_engine: Engine) -> None:
        definitions = PostgresExternalStrategyDefinitionRepository(postgres_engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(postgres_engine)
        candles = _FakeCandleSource({"c1": _candles([str(100 + i) for i in range(40)])})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=40)
        evaluation = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        PromoteExternalStrategy(
            definitions=definitions,
            evaluations=evaluations,
            provenance=PostgresStrategyVersionProvenanceRepository(postgres_engine),
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            evaluation_id=evaluation.record.evaluation_id,
            promoted_at=NOW,
        )
        snapshot = external_strategy_registry_snapshot(
            definitions=definitions, evaluations=evaluations
        )
        row = next(
            item for item in snapshot["strategies"] if item["strategy_id"] == definition.strategy_id
        )
        assert row["lifecycle_state"] == "PAPER_ELIGIBLE"
        assert row["version"] == 2

        detail = external_strategy_detail(
            definitions=definitions, evaluations=evaluations, strategy_id=definition.strategy_id
        )
        assert len(detail["versions"]) == 2
        assert len(detail["evaluations"]) == 1
