"""Fix-independent gap demonstrations for Strategy Version Provenance v1.

Written against the APIs that existed at ``d313993`` (commit 4b6d794). G3 and
G4 were ``xfail(strict=True)`` there. The fix commit removed their markers,
and G4 now also passes the provenance repository the fixed service requires.
G2, G7, and G9 demonstrate lossy state that the fix deliberately leaves in
place: it adds bindings and never rewrites existing hashes. See
``docs/STRATEGY_VERSION_PROVENANCE_V1.md`` (gaps G1-G9) for the
reconstruction behind each one.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

import apex.application.strategy_evaluation_history as history_application
from apex.application.strategy_evaluation_history import (
    InMemoryInternalStrategyEvaluationRepository,
    InMemoryInternalStrategyPromotionRepository,
    PromoteInternalStrategy,
    RecordInternalStrategyEvaluation,
    StrategyEvaluationHistoryResearchError,
)
from apex.application.strategy_version_provenance import (
    InMemoryStrategyVersionProvenanceRepository,
    RecordEvaluationSubject,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.external_strategy import (
    EXTERNAL_STRATEGY_SPECIFICATION_V1,
    strategy_definition_from_specification,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyLifecycleState,
    next_version,
)
from apex.domain.strategy_evaluation import (
    StrategyEvaluationError,
    evaluate_external_strategy,
    promote_to_paper_eligible,
)
from apex.domain.strategy_evaluation_history import build_internal_strategy_evaluation_record
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionRegistry,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CAPTURE_ID = "test-capture-strategy-version-provenance-gaps"
CYCLE_ID = "recurring-strategy-paper-plan-v1-worker-20260902"


def _candles(count: int) -> tuple[CryptoDailyCandle, ...]:
    return tuple(
        CryptoDailyCandle.create(
            capture_id=CAPTURE_ID,
            attempt_id=f"attempt-{offset}",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=_EPOCH + timedelta(days=offset),
            open=str(100 + 5 * offset),
            high=str(100 + 5 * offset),
            low=str(100 + 5 * offset),
            close=str(100 + 5 * offset),
            volume="1",
            ingested_at=NOW,
            raw_response_hash="a" * 64,
        )
        for offset in range(count)
    )


def _buy_and_hold() -> StrategyDefinition:
    return canonical_strategy_benchmarks(created_at=NOW)[0]


def _edited(definition: StrategyDefinition, *, display_name: str) -> StrategyDefinition:
    """The same strategy after a descriptive catalog edit: same strategy_id and
    version, but a different artifact (different canonical_hash)."""

    return StrategyDefinition.create(
        source_kind=definition.source_kind,
        display_name=display_name,
        venue=definition.venue,
        instrument_ids=definition.instrument_ids,
        horizon_kind=definition.horizon_kind,
        rule_kind=definition.rule_kind,
        rule_parameters=definition.rule_parameters,
        evidence_requirements=definition.evidence_requirements,
        provenance=definition.provenance,
        created_at=definition.created_at,
    )


def _evaluate(definition: StrategyDefinition):  # type: ignore[no-untyped-def]
    backtest = simulate_strategy_backtest(
        definition,
        _candles(35),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_provenance=CAPTURE_ID,
    )
    return evaluate_external_strategy(definition, backtest)


def test_baseline_fact_an_edited_definition_keeps_strategy_id_and_version() -> None:
    """Not a gap test: pins the premise the gap tests below rely on."""

    version_a = _buy_and_hold()
    version_b = _edited(version_a, display_name="Buy and Hold BTC-USD (renamed)")
    assert (version_a.strategy_id, version_a.version) == (version_b.strategy_id, version_b.version)
    assert version_a.canonical_hash != version_b.canonical_hash


def test_g2_evaluation_record_cannot_identify_the_evaluated_artifact() -> None:
    """Gap G2 (requirement 2): two different artifacts sharing (strategy_id,
    version) evaluated on identical evidence yield evaluation records that do
    not name either artifact's hash. Resolving the recommendation to an
    artifact by (strategy_id, version), family, or name is therefore
    ambiguous. The fix adds a separate subject binding and leaves this record
    and its hash byte-identical, so this demonstration keeps passing after the
    fix, by design."""

    version_a = _buy_and_hold()
    version_b = _edited(version_a, display_name="Buy and Hold BTC-USD (renamed)")
    records = [
        build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=_evaluate(definition),
            cycle_id=CYCLE_ID,
            as_of=NOW,
            capture_id=CAPTURE_ID,
            evaluated_at=NOW,
        )
        for definition in (version_a, version_b)
    ]
    for record in records:
        retained = {str(value) for value in dataclasses.asdict(record).values()}
        assert version_a.canonical_hash not in retained
        assert version_b.canonical_hash not in retained
    assert {(item.strategy_id, item.strategy_version) for item in records} == {
        (version_a.strategy_id, 1)
    }


def test_g3_promotion_rejects_an_evaluation_of_a_different_version() -> None:
    """Requirement 4: promotion cannot bind to a version other than the one
    evaluated. An evaluation of v2 must never promote v1."""

    version_one = _buy_and_hold()
    version_two = next_version(
        version_one, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
    )
    evaluation_of_v2 = _evaluate(version_two)
    assert evaluation_of_v2.strategy_version == 2
    with pytest.raises(StrategyEvaluationError, match="strategy version"):
        promote_to_paper_eligible(version_one, evaluation=evaluation_of_v2, promoted_at=NOW)


def test_g4_operator_promotion_fails_closed_when_current_catalog_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 1: version A was evaluated; the catalog later defines B
    (same id and version, different artifact). Promotion must still identify
    A. Since A is no longer what the catalog builds, promotion must fail
    closed instead of silently promoting B on A's evidence."""

    version_a = _buy_and_hold()
    evaluations = InMemoryInternalStrategyEvaluationRepository()
    recorded = RecordInternalStrategyEvaluation(evaluations).execute(
        definition=version_a,
        candidate=_evaluate(version_a),
        cycle_id=CYCLE_ID,
        as_of=NOW,
        capture_id=CAPTURE_ID,
        evaluated_at=NOW,
    )
    provenance = InMemoryStrategyVersionProvenanceRepository()
    RecordEvaluationSubject(provenance).execute(
        definition=version_a,
        registry=StrategyVersionRegistry.CATALOG_CODE,
        evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
        evaluation_id=recorded.record.evaluation_id,
        evaluation_hash=recorded.record.canonical_hash,
        evaluated_strategy_id=recorded.record.strategy_id,
        evaluated_strategy_version=recorded.record.strategy_version,
    )
    version_b = _edited(version_a, display_name="Buy and Hold BTC-USD (catalog edited later)")
    original_catalog = history_application.canonical_strategy_benchmarks

    def edited_catalog(*, created_at: datetime) -> tuple[StrategyDefinition, ...]:
        return (version_b, *original_catalog(created_at=created_at)[1:])

    monkeypatch.setattr(history_application, "canonical_strategy_benchmarks", edited_catalog)
    promotions = InMemoryInternalStrategyPromotionRepository()
    with pytest.raises(
        StrategyEvaluationHistoryResearchError, match="DIFFERS_FROM_EVALUATED_ARTIFACT"
    ):
        PromoteInternalStrategy(
            evaluations=evaluations, promotions=promotions, provenance=provenance
        ).execute(
            strategy_id=version_a.strategy_id,
            evaluation_id=recorded.record.evaluation_id,
            promoted_at=NOW + timedelta(days=1),
            actor="operator:test",
        )
    assert promotions.for_strategy(strategy_id=version_a.strategy_id) == ()
    assert provenance.transitions_for_strategy(strategy_id=version_a.strategy_id) == ()


def test_g7_successor_lineage_names_itself_not_its_parent_artifact() -> None:
    """Gap G7 (requirement 6 prerequisite): a successor's only lineage field,
    ``parent_strategy_id``, equals its own ``strategy_id``. It records neither
    the parent's version nor its hash. The fix records the exact parent
    artifact in ``strategy_version`` and deliberately leaves
    ``StrategyDefinition`` unchanged: changing its payload would change every
    v2 hash, including the one inside the live trial's retained authorization."""

    version_one = _buy_and_hold()
    version_two = next_version(
        version_one, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
    )
    assert version_two.parent_strategy_id == version_two.strategy_id
    assert version_one.canonical_hash not in {
        str(value) for value in version_two.canonical_payload.values()
    }


def test_g9_baseline_fact_external_and_catalog_share_a_strategy_id() -> None:
    """Not a gap test: pins the cross-registry collision (G9) that
    provenance identity must disambiguate. Provenance handles it by keying on
    registry; it never changes strategy_id."""

    catalog = _buy_and_hold()
    external = strategy_definition_from_specification(
        {
            "specification_version": EXTERNAL_STRATEGY_SPECIFICATION_V1,
            "display_name": "Operator buy and hold",
            "venue": "CRYPTO",
            "instrument_id": "BTC-USD",
            "rule_kind": "BUY_AND_HOLD",
            "rule_parameters": {},
            "reference_description": "Operator-registered buy and hold.",
            "origin_note": "test",
        },
        created_at=NOW,
    )
    assert external.strategy_id == catalog.strategy_id
    assert external.canonical_hash != catalog.canonical_hash
