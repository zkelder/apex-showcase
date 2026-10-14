from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.external_strategy_research import (
    EvaluateExternalStrategy,
    ExternalStrategyResearchError,
    InMemoryExternalStrategyDefinitionRepository,
    InMemoryExternalStrategyEvaluationRepository,
    PromoteExternalStrategy,
    RegisterExternalStrategy,
    external_strategy_detail,
    external_strategy_registry_snapshot,
)
from apex.application.strategy_version_provenance import (
    InMemoryStrategyVersionProvenanceRepository,
)
from apex.domain.crypto_campaign import FIXED_HOLDOUT_CAPTURE_ID
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.external_strategy import (
    EXTERNAL_STRATEGY_SPECIFICATION_V1,
    ExternalStrategyEvaluationError,
    ExternalStrategyQualification,
    ExternalStrategySpecificationError,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import BENCHMARK_INSTRUMENT, canonical_strategy_benchmarks
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)
_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class _FakeCandleSource:
    """Minimal duck-typed stand-in for ``application.crypto_research.
    CryptoResearchRepository`` -- this module's ``EvaluateExternalStrategy``
    only ever calls ``.candles(capture_id)`` on what it is given, so a tiny
    local fake keeps these tests decoupled from that module's own
    construction details."""

    def __init__(self, by_capture_id: dict[str, tuple[CryptoDailyCandle, ...]]) -> None:
        self._by_capture_id = by_capture_id

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        return self._by_capture_id.get(capture_id, ())


def _candles(prices: list[str], *, start: datetime = _EPOCH) -> tuple[CryptoDailyCandle, ...]:
    candles = []
    for offset, price in enumerate(prices):
        candles.append(
            CryptoDailyCandle.create(
                capture_id="test-capture",
                attempt_id="test-attempt",
                evidence_class=EvidenceClass.FIXTURE,
                bucket_start=start + timedelta(days=offset),
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


def _spec(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "specification_version": EXTERNAL_STRATEGY_SPECIFICATION_V1,
        "display_name": "20/50-Day Moving Average Crossover (Manual Demo)",
        "venue": "CRYPTO",
        "instrument_id": BENCHMARK_INSTRUMENT,
        "rule_kind": "MOVING_AVERAGE_TREND",
        "rule_parameters": {"fast_window_days": 20, "slow_window_days": 50},
        "reference_description": "A well-known short/medium-term moving-average crossover rule.",
        "origin_note": (
            "Manually transcribed by an operator; not sourced from one specific publication."
        ),
    }
    base.update(overrides)
    return base


def _buy_and_hold_spec(**overrides: object) -> dict[str, object]:
    return _spec(
        display_name="Buy and Hold BTC-USD (Manual Demo)",
        rule_kind="BUY_AND_HOLD",
        rule_parameters={},
        **overrides,
    )


class TestRegisterExternalStrategy:
    def test_registers_a_valid_specification(self) -> None:
        repo = InMemoryExternalStrategyDefinitionRepository()
        result = RegisterExternalStrategy(repo).execute(specification=_spec(), created_at=NOW)
        assert result.idempotent_replay is False
        assert repo.get(strategy_id=result.definition.strategy_id, version=1) == result.definition

    def test_idempotent_on_replay(self) -> None:
        repo = InMemoryExternalStrategyDefinitionRepository()
        service = RegisterExternalStrategy(repo)
        first = service.execute(specification=_spec(), created_at=NOW)
        second = service.execute(specification=_spec(), created_at=NOW + timedelta(days=1))
        assert first.idempotent_replay is False
        assert second.idempotent_replay is True
        assert first.definition.strategy_id == second.definition.strategy_id
        assert len(repo.versions(strategy_id=first.definition.strategy_id)) == 1

    def test_raises_on_genuine_identity_conflict(self) -> None:
        repo = InMemoryExternalStrategyDefinitionRepository()
        service = RegisterExternalStrategy(repo)
        service.execute(specification=_spec(), created_at=NOW)
        with pytest.raises(ExternalStrategyResearchError):
            service.execute(
                specification=_spec(display_name="A materially different name"), created_at=NOW
            )

    def test_malformed_specification_propagates_a_clear_error(self) -> None:
        repo = InMemoryExternalStrategyDefinitionRepository()
        with pytest.raises(ExternalStrategySpecificationError):
            RegisterExternalStrategy(repo).execute(
                specification=_spec(rule_kind="NOT_A_RULE"), created_at=NOW
            )


class TestEvaluateExternalStrategy:
    def _service(
        self, *, candles_by_capture: dict[str, tuple[CryptoDailyCandle, ...]] | None = None
    ) -> tuple[
        EvaluateExternalStrategy,
        InMemoryExternalStrategyDefinitionRepository,
        InMemoryExternalStrategyEvaluationRepository,
    ]:
        definitions = InMemoryExternalStrategyDefinitionRepository()
        evaluations = InMemoryExternalStrategyEvaluationRepository()
        candles = _FakeCandleSource(candles_by_capture or {})
        return (
            EvaluateExternalStrategy(
                definitions=definitions, evaluations=evaluations, candles=candles
            ),
            definitions,
            evaluations,
        )

    def _register(
        self, definitions: InMemoryExternalStrategyDefinitionRepository, **overrides: object
    ) -> StrategyDefinition:
        return (
            RegisterExternalStrategy(definitions)
            .execute(specification=_spec(**overrides), created_at=NOW)
            .definition
        )

    def test_raises_when_definition_is_not_registered(self) -> None:
        service, _definitions, _evaluations = self._service()
        with pytest.raises(ExternalStrategyResearchError):
            service.execute(
                strategy_id="nonexistent",
                version=1,
                capture_id="c",
                as_of=NOW,
                evaluated_at=NOW,
            )

    def test_rejects_a_multi_instrument_definition(self) -> None:
        definitions = InMemoryExternalStrategyDefinitionRepository()
        evaluations = InMemoryExternalStrategyEvaluationRepository()
        multi = StrategyDefinition.create(
            source_kind=StrategySourceKind.EXTERNAL,
            display_name="Not supported",
            venue=SharedVenue.CRYPTO,
            instrument_ids=(BENCHMARK_INSTRUMENT, "ETH-USD"),
            horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
            rule_kind=StrategyRuleKind.BUY_AND_HOLD,
            rule_parameters=StrategyRuleParameters(),
            evidence_requirements=StrategyEvidenceRequirements(
                minimum_history_days=1, requires_contemporaneous_observation=False
            ),
            provenance=StrategyProvenance(
                reference_kind="PUBLISHED_CONVENTION", reference_description="x", origin_note="x"
            ),
            created_at=NOW,
        )
        definitions.register(definition=multi)
        service = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=_FakeCandleSource({})
        )
        with pytest.raises(ExternalStrategyEvaluationError):
            service.execute(
                strategy_id=multi.strategy_id,
                version=1,
                capture_id="c",
                as_of=NOW,
                evaluated_at=NOW,
            )

    def test_rejects_the_sealed_holdout_capture(self) -> None:
        service, definitions, _evaluations = self._service()
        definition = self._register(definitions)
        with pytest.raises(ExternalStrategyEvaluationError):
            service.execute(
                strategy_id=definition.strategy_id,
                version=1,
                capture_id=FIXED_HOLDOUT_CAPTURE_ID,
                as_of=NOW,
                evaluated_at=NOW,
            )

    def test_rejects_a_capture_with_no_retained_candles(self) -> None:
        service, definitions, _evaluations = self._service()
        definition = self._register(definitions)
        with pytest.raises(ExternalStrategyEvaluationError):
            service.execute(
                strategy_id=definition.strategy_id,
                version=1,
                capture_id="unknown-capture",
                as_of=NOW,
                evaluated_at=NOW,
            )

    def test_rejects_an_as_of_before_any_retained_candle(self) -> None:
        prices = [str(100 + i) for i in range(60)]
        service, definitions, _evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions)
        with pytest.raises(ExternalStrategyEvaluationError):
            service.execute(
                strategy_id=definition.strategy_id,
                version=1,
                capture_id="c1",
                as_of=_EPOCH - timedelta(days=1),
                evaluated_at=NOW,
            )

    def test_insufficient_history_for_the_candidate_rule_fails_closed(self) -> None:
        prices = [str(100 + i) for i in range(10)]  # far fewer than the 51 MA needs
        service, definitions, _evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions)
        as_of = _EPOCH + timedelta(days=len(prices) + 5)
        with pytest.raises(ExternalStrategyEvaluationError):
            service.execute(
                strategy_id=definition.strategy_id,
                version=1,
                capture_id="c1",
                as_of=as_of,
                evaluated_at=NOW,
            )

    def test_deterministic_replay_and_idempotency(self) -> None:
        prices = [str(100 + i) for i in range(90)]
        service, definitions, evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions)
        as_of = _EPOCH + timedelta(days=len(prices))
        first = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        second = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW + timedelta(hours=6),
        )
        assert first.idempotent_replay is False
        assert second.idempotent_replay is True
        assert first.record.evaluation_id == second.record.evaluation_id
        assert first.record.canonical_hash == second.record.canonical_hash
        assert len(evaluations.for_strategy(strategy_id=definition.strategy_id)) == 1

    def test_future_candles_beyond_as_of_do_not_change_the_persisted_result(self) -> None:
        prices = [str(100 + i) for i in range(90)]
        as_of = _EPOCH + timedelta(days=len(prices))
        baseline_service, baseline_definitions, _e = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        baseline_definition = self._register(baseline_definitions)
        baseline_result = baseline_service.execute(
            strategy_id=baseline_definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )

        leaking_prices = [*prices, "1", "1", "50000", "50000"]
        leaking_service, leaking_definitions, _e2 = self._service(
            candles_by_capture={"c1": _candles(leaking_prices)}
        )
        leaking_definition = self._register(leaking_definitions)
        leaking_result = leaking_service.execute(
            strategy_id=leaking_definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,  # identical as_of -- the extra future candles must not matter
            evaluated_at=NOW,
        )

        assert baseline_result.record.canonical_hash == leaking_result.record.canonical_hash
        assert (
            baseline_result.record.candidate.net_return
            == leaking_result.record.candidate.net_return
        )

    def test_cost_assumptions_are_applied_gross_differs_from_net(self) -> None:
        prices = [str(100 + i) for i in range(90)]
        service, definitions, _evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions)
        as_of = _EPOCH + timedelta(days=len(prices))
        result = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        candidate = result.record.candidate
        assert candidate.total_fees is not None and candidate.total_fees > 0
        assert candidate.net_return != candidate.gross_return

    def test_benchmark_comparisons_are_computed_against_the_canonical_set(self) -> None:
        prices = [str(Decimal("100") + i) for i in range(400)]
        service, definitions, _evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions)
        as_of = _EPOCH + timedelta(days=len(prices))
        result = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        canonical_ids = {item.strategy_id for item in canonical_strategy_benchmarks(created_at=NOW)}
        assert result.record.benchmark_comparisons
        for comparison in result.record.benchmark_comparisons:
            assert comparison.benchmark_strategy_id in canonical_ids
            assert comparison.candidate_strategy_id == definition.strategy_id
        assert (
            set(result.compared_benchmark_strategy_ids) | set(result.skipped_benchmark_strategy_ids)
            == canonical_ids
        )

    def test_a_weak_strategy_remains_research_only(self) -> None:
        # Monotonically falling prices: buy-and-hold from day one loses
        # money net of costs -- genuinely weak evidence, not engineered
        # around the qualification threshold.
        prices = [str(200 - i) for i in range(40)]
        service, definitions, _evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions, **_buy_and_hold_spec())
        as_of = _EPOCH + timedelta(days=len(prices))
        result = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        assert (
            result.record.qualification
            is not ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        assert definition.lifecycle_state.value == "RESEARCH_ONLY"

    def test_a_strong_strategy_would_qualify_and_can_reach_paper_eligible(self) -> None:
        # Monotonically rising prices, buy-and-hold, plenty of days --
        # unambiguous positive net return with sample_size well past the
        # 30-observation floor.
        prices = [str(100 + i) for i in range(40)]
        service, definitions, evaluations = self._service(
            candles_by_capture={"c1": _candles(prices)}
        )
        definition = self._register(definitions, **_buy_and_hold_spec())
        as_of = _EPOCH + timedelta(days=len(prices))
        result = service.execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        assert (
            result.record.qualification
            is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        )
        assert result.record.candidate.sample_size >= 30

        # Evaluating alone must never itself move the definition off
        # RESEARCH_ONLY -- reaching PAPER_ELIGIBLE requires the separate,
        # explicit PromoteExternalStrategy action (Part 12/13).
        assert (
            definitions.get(strategy_id=definition.strategy_id, version=1).lifecycle_state.value
            == "RESEARCH_ONLY"
        )

        provenance = InMemoryStrategyVersionProvenanceRepository()
        promotion = PromoteExternalStrategy(
            definitions=definitions, evaluations=evaluations, provenance=provenance
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            evaluation_id=result.record.evaluation_id,
            promoted_at=NOW,
        )
        assert promotion.definition.lifecycle_state.value == "PAPER_ELIGIBLE"
        assert promotion.definition.version == 2
        assert promotion.definition.strategy_id == definition.strategy_id


class TestPromoteExternalStrategy:
    def test_rejects_unknown_definition(self) -> None:
        definitions = InMemoryExternalStrategyDefinitionRepository()
        evaluations = InMemoryExternalStrategyEvaluationRepository()
        with pytest.raises(ExternalStrategyResearchError):
            PromoteExternalStrategy(
                definitions=definitions,
                evaluations=evaluations,
                provenance=InMemoryStrategyVersionProvenanceRepository(),
            ).execute(strategy_id="nope", version=1, evaluation_id="nope", promoted_at=NOW)

    def test_rejects_unknown_evaluation(self) -> None:
        definitions = InMemoryExternalStrategyDefinitionRepository()
        evaluations = InMemoryExternalStrategyEvaluationRepository()
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_buy_and_hold_spec(), created_at=NOW)
            .definition
        )
        with pytest.raises(ExternalStrategyResearchError):
            PromoteExternalStrategy(
                definitions=definitions,
                evaluations=evaluations,
                provenance=InMemoryStrategyVersionProvenanceRepository(),
            ).execute(
                strategy_id=definition.strategy_id, version=1, evaluation_id="nope", promoted_at=NOW
            )

    def test_rejects_an_evaluation_belonging_to_a_different_strategy(self) -> None:
        prices = [str(100 + i) for i in range(40)]
        definitions = InMemoryExternalStrategyDefinitionRepository()
        evaluations = InMemoryExternalStrategyEvaluationRepository()
        candles = _FakeCandleSource({"c1": _candles(prices)})
        service = EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        )

        strong = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_buy_and_hold_spec(), created_at=NOW)
            .definition
        )
        other = (
            RegisterExternalStrategy(definitions)
            .execute(
                specification=_spec(display_name="A different strategy entirely"), created_at=NOW
            )
            .definition
        )
        as_of = _EPOCH + timedelta(days=len(prices))
        result = service.execute(
            strategy_id=strong.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )
        with pytest.raises(ExternalStrategyResearchError):
            PromoteExternalStrategy(
                definitions=definitions,
                evaluations=evaluations,
                provenance=InMemoryStrategyVersionProvenanceRepository(),
            ).execute(
                strategy_id=other.strategy_id,
                version=1,
                evaluation_id=result.record.evaluation_id,
                promoted_at=NOW,
            )

    def test_never_authorizes_a_paper_trial_structurally(self) -> None:
        """``PromoteExternalStrategy`` accepts no PAPER-account/execution
        dependency at all -- it cannot call ``application.strategy_paper_
        trial.ExecuteStrategyPaperTrial`` because it never holds a reference
        to anything that service needs (Part 13)."""

        import inspect

        parameters = inspect.signature(PromoteExternalStrategy.__init__).parameters
        # Strategy Version Provenance v1 adds only a lineage repository typed
        # as the narrow StrategyVersionProvenanceRepository, which has no
        # trial-opening method (see ProvenancedTrialRepository).
        assert set(parameters) == {"self", "definitions", "evaluations", "provenance"}


class TestReadModel:
    def test_registry_snapshot_and_detail_reflect_registered_strategies(self) -> None:
        prices = [str(100 + i) for i in range(40)]
        definitions = InMemoryExternalStrategyDefinitionRepository()
        evaluations = InMemoryExternalStrategyEvaluationRepository()
        candles = _FakeCandleSource({"c1": _candles(prices)})
        definition = (
            RegisterExternalStrategy(definitions)
            .execute(specification=_buy_and_hold_spec(), created_at=NOW)
            .definition
        )
        as_of = _EPOCH + timedelta(days=len(prices))
        EvaluateExternalStrategy(
            definitions=definitions, evaluations=evaluations, candles=candles
        ).execute(
            strategy_id=definition.strategy_id,
            version=1,
            capture_id="c1",
            as_of=as_of,
            evaluated_at=NOW,
        )

        snapshot = external_strategy_registry_snapshot(
            definitions=definitions, evaluations=evaluations
        )
        assert snapshot["total"] == 1
        row = snapshot["strategies"][0]
        assert row["source_kind"] == "EXTERNAL"
        assert row["lifecycle_state"] == "RESEARCH_ONLY"
        assert row["latest_evaluation"] is not None
        assert row["latest_evaluation"]["qualification"] == "WOULD_QUALIFY_FOR_PAPER_ELIGIBLE"

        detail = external_strategy_detail(
            definitions=definitions, evaluations=evaluations, strategy_id=definition.strategy_id
        )
        assert len(detail["versions"]) == 1
        assert len(detail["evaluations"]) == 1

    def test_empty_registry_reports_zero_strategies(self) -> None:
        snapshot = external_strategy_registry_snapshot(
            definitions=InMemoryExternalStrategyDefinitionRepository(),
            evaluations=InMemoryExternalStrategyEvaluationRepository(),
        )
        assert snapshot["total"] == 0
        assert snapshot["strategies"] == ()
