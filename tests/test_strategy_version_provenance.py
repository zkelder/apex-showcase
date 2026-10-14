"""Strategy Version Provenance v1: domain and application contract tests.

Requirement numbers refer to the milestone brief (see
``docs/STRATEGY_VERSION_PROVENANCE_V1.md`` Part 3):
1 version A evaluated, catalog later B  ->  lineage still names A
2 two artifacts of one family coexist   ->  never resolved by name/version
3 replay                                ->  byte-identical provenance
4 promotion cannot bind another version
5 PAPER eligibility keeps version provenance
6 trial traceable to originating version
7 historical rows unchanged after later revisions
8 missing/invalid provenance fails closed
9 legacy generation-1 evidence stays readable and explicitly classified
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from apex.application.strategy_evaluation_history import (
    InMemoryInternalStrategyEvaluationRepository,
    RecordInternalStrategyEvaluation,
    RecordInternalStrategyEvaluationResult,
)
from apex.application.strategy_paper_trial_lifecycle import InMemoryStrategyPaperTrialRepository
from apex.application.strategy_version_provenance import (
    InMemoryStrategyVersionProvenanceRepository,
    RecordEvaluationSubject,
    RecordProvenancedTrialOpened,
    RecordStrategyVersionTransition,
    StrategyVersionProvenanceResearchError,
    strategy_version_provenance_report,
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
    StrategyEvaluationResult,
    evaluate_external_strategy,
    promote_to_active,
    promote_to_paper_eligible,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialRecord,
    build_strategy_paper_trial_record,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionProvenanceError,
    StrategyVersionRef,
    StrategyVersionRegistry,
    StrategyVersionTransitionKind,
    TrialProvenanceClass,
    build_strategy_evaluation_subject,
    build_strategy_paper_trial_provenance,
    build_strategy_version_record,
    build_strategy_version_transition,
    classify_trial_provenance,
    definition_artifact_bytes,
    rehydrate_strategy_version_record,
    rehydrate_strategy_version_transition,
)

NOW = datetime(2026, 9, 2, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CAPTURE_ID = "test-capture-strategy-version-provenance"
CATALOG = StrategyVersionRegistry.CATALOG_CODE
INTERNAL = StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION
ADMISSION = StrategyVersionTransitionKind.CYCLE_GATE_ADMISSION


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


def _buy_and_hold(created_at: datetime = NOW) -> StrategyDefinition:
    return canonical_strategy_benchmarks(created_at=created_at)[0]


def _edited(definition: StrategyDefinition, *, display_name: str) -> StrategyDefinition:
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


def _evaluate(definition: StrategyDefinition, count: int = 35) -> StrategyEvaluationResult:
    backtest = simulate_strategy_backtest(
        definition,
        _candles(count),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_provenance=CAPTURE_ID,
    )
    return evaluate_external_strategy(definition, backtest)


class _Cycle:
    """The recurring cycle's write sequence, reproduced on in-memory repos."""

    def __init__(self) -> None:
        self.evaluations = InMemoryInternalStrategyEvaluationRepository()
        self.trials = InMemoryStrategyPaperTrialRepository()
        self.provenance = InMemoryStrategyVersionProvenanceRepository(trials=self.trials)

    def evaluate(
        self, definition: StrategyDefinition, *, as_of: datetime = NOW
    ) -> tuple[RecordInternalStrategyEvaluationResult, StrategyEvaluationResult]:
        evaluation = _evaluate(definition)
        recorded = RecordInternalStrategyEvaluation(self.evaluations).execute(
            definition=definition,
            candidate=evaluation,
            cycle_id=f"cycle-{as_of.date()}",
            as_of=as_of,
            capture_id=CAPTURE_ID,
            evaluated_at=as_of,
        )
        RecordEvaluationSubject(self.provenance).execute(
            definition=definition,
            registry=CATALOG,
            evaluation_source=INTERNAL,
            evaluation_id=recorded.record.evaluation_id,
            evaluation_hash=recorded.record.canonical_hash,
            evaluated_strategy_id=recorded.record.strategy_id,
            evaluated_strategy_version=recorded.record.strategy_version,
        )
        return recorded, evaluation

    def admit(self, definition: StrategyDefinition, *, as_of: datetime = NOW):  # type: ignore[no-untyped-def]
        recorded, evaluation = self.evaluate(definition, as_of=as_of)
        promoted = promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=as_of)
        admission = RecordStrategyVersionTransition(self.provenance).execute(
            kind=ADMISSION,
            from_definition=definition,
            to_definition=promoted,
            evaluation_source=INTERNAL,
            evaluation_id=recorded.record.evaluation_id,
            evaluation=evaluation,
            registry=CATALOG,
        )
        return recorded, promoted, admission


def _trial(
    promoted: StrategyDefinition, *, generation: int = 1, legacy_adopted: bool = False
) -> StrategyPaperTrialRecord:
    return build_strategy_paper_trial_record(
        strategy_id=promoted.strategy_id,
        strategy_version=promoted.version,
        instrument_id=promoted.instrument_ids[0],
        account_id="genuine-strategy-crypto-paper-account",
        generation=generation,
        opened_at=NOW,
        opening_authorization_id=f"auth-{generation}",
        opening_order_id=f"order-{generation}",
        opening_realized_pnl_baseline=Decimal("0"),
        legacy_adopted=legacy_adopted,
    )


class TestStrategyVersionRecord:
    def test_artifact_bytes_hash_to_the_definition_hash(self) -> None:
        definition = _buy_and_hold()
        artifact = definition_artifact_bytes(definition)
        assert sha256(artifact).hexdigest() == definition.canonical_hash
        decoded = json.loads(artifact)
        assert decoded["strategy_id"] == definition.strategy_id
        assert decoded["version"] == 1
        assert decoded["lifecycle_state"] == "RESEARCH_ONLY"

    def test_replay_on_another_day_is_byte_identical(self) -> None:
        """Requirement 3: created_at never enters identity."""

        first = build_strategy_version_record(
            definition=_buy_and_hold(NOW), registry=CATALOG, parent=None
        )
        second = build_strategy_version_record(
            definition=_buy_and_hold(NOW + timedelta(days=30)), registry=CATALOG, parent=None
        )
        assert first == second

    def test_successor_records_its_exact_parent_artifact(self) -> None:
        root = _buy_and_hold()
        promoted = next_version(
            root, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=LATER
        )
        record = build_strategy_version_record(definition=promoted, registry=CATALOG, parent=root)
        assert record.parent_ref == StrategyVersionRef.of(root, registry=CATALOG)
        assert record.ref.version == 2

    def test_active_successor_is_recognised_as_derived(self) -> None:
        root = _buy_and_hold()
        evaluation = _evaluate(root)
        eligible = promote_to_paper_eligible(root, evaluation=evaluation, promoted_at=NOW)
        active = promote_to_active(eligible, evaluation=_evaluate(eligible), promoted_at=LATER)
        record = build_strategy_version_record(definition=active, registry=CATALOG, parent=eligible)
        assert record.lifecycle_state is StrategyLifecycleState.ACTIVE

    def test_successor_must_be_derived_from_the_named_parent(self) -> None:
        """Requirement 1/4: a v2 derived from A cannot claim B as its parent."""

        version_a = _buy_and_hold()
        version_b = _edited(version_a, display_name="edited later")
        promoted_from_a = next_version(
            version_a, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        with pytest.raises(StrategyVersionProvenanceError, match="NOT_DERIVED_FROM_PARENT"):
            build_strategy_version_record(
                definition=promoted_from_a, registry=CATALOG, parent=version_b
            )

    def test_root_iff_version_one(self) -> None:
        root = _buy_and_hold()
        promoted = next_version(
            root, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        with pytest.raises(StrategyVersionProvenanceError, match="ROOT_IFF"):
            build_strategy_version_record(definition=promoted, registry=CATALOG, parent=None)
        with pytest.raises(StrategyVersionProvenanceError, match="ROOT_IFF"):
            build_strategy_version_record(definition=root, registry=CATALOG, parent=root)

    def test_rehydrate_detects_tampered_artifact_or_hash(self) -> None:
        """Requirement 8: stored bytes that do not hash to the ref fail closed."""

        record = build_strategy_version_record(
            definition=_buy_and_hold(), registry=CATALOG, parent=None
        )
        with pytest.raises(StrategyVersionProvenanceError, match="ARTIFACT_HASH"):
            rehydrate_strategy_version_record(
                ref=record.ref,
                lifecycle_state=record.lifecycle_state,
                parent_ref=None,
                artifact=record.artifact.replace(b"Buy", b"Sell"),
                canonical_hash=record.canonical_hash,
            )
        with pytest.raises(StrategyVersionProvenanceError, match="RECORD_HASH"):
            rehydrate_strategy_version_record(
                ref=record.ref,
                lifecycle_state=record.lifecycle_state,
                parent_ref=None,
                artifact=record.artifact,
                canonical_hash="0" * 64,
            )


class TestEvaluationSubject:
    def test_two_artifacts_of_one_family_bind_to_their_own_evaluations(self) -> None:
        """Requirement 2: same (strategy_id, version), different artifacts;
        each evaluation resolves to its exact artifact, never by name."""

        cycle = _Cycle()
        version_a = _buy_and_hold()
        version_b = _edited(version_a, display_name="edited later")
        recorded_a, _ = cycle.evaluate(version_a, as_of=NOW)
        recorded_b, _ = cycle.evaluate(version_b, as_of=LATER)
        subject_a = cycle.provenance.subject_for(
            evaluation_source=INTERNAL, evaluation_id=recorded_a.record.evaluation_id
        )
        subject_b = cycle.provenance.subject_for(
            evaluation_source=INTERNAL, evaluation_id=recorded_b.record.evaluation_id
        )
        assert subject_a is not None and subject_b is not None
        assert subject_a.subject.definition_hash == version_a.canonical_hash
        assert subject_b.subject.definition_hash == version_b.canonical_hash
        report = strategy_version_provenance_report(
            provenance=cycle.provenance, strategy_id=version_a.strategy_id
        )
        assert report["warnings"] == ["MULTIPLE_ARTIFACTS_FOR_VERSION:CATALOG_CODE:1:2"]

    def test_subject_must_match_the_evaluation_record_version(self) -> None:
        with pytest.raises(StrategyVersionProvenanceError, match="SUBJECT_MISMATCH"):
            build_strategy_evaluation_subject(
                evaluation_source=INTERNAL,
                evaluation_id="internal_strategy_evaluation_x",
                evaluation_hash="a" * 64,
                evaluated_strategy_id=_buy_and_hold().strategy_id,
                evaluated_strategy_version=2,
                definition=_buy_and_hold(),
                registry=CATALOG,
            )

    def test_cross_registry_collision_is_disambiguated(self) -> None:
        """G9: an operator BUY_AND_HOLD shares the catalog strategy_id but is
        a different artifact in a different registry, and an internal
        evaluation can never bind to the external registry."""

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
        catalog_ref = StrategyVersionRef.of(catalog, registry=CATALOG)
        external_ref = StrategyVersionRef.of(
            external, registry=StrategyVersionRegistry.EXTERNAL_OPERATOR
        )
        assert catalog_ref != external_ref
        with pytest.raises(StrategyVersionProvenanceError, match="SOURCE_REGISTRY"):
            build_strategy_evaluation_subject(
                evaluation_source=INTERNAL,
                evaluation_id="internal_strategy_evaluation_x",
                evaluation_hash="a" * 64,
                evaluated_strategy_id=external.strategy_id,
                evaluated_strategy_version=1,
                definition=external,
                registry=StrategyVersionRegistry.EXTERNAL_OPERATOR,
            )


class TestTransition:
    def test_admission_binds_the_exact_evaluated_and_admitted_artifacts(self) -> None:
        """Requirements 5/6: PAPER eligibility keeps the exact v1 and v2 hashes."""

        cycle = _Cycle()
        root = _buy_and_hold()
        recorded, promoted, admission = cycle.admit(root)
        transition = admission.transition
        assert transition.from_ref == StrategyVersionRef.of(root, registry=CATALOG)
        assert transition.to_ref == StrategyVersionRef.of(promoted, registry=CATALOG)
        assert transition.evaluation_id == recorded.record.evaluation_id
        assert transition.evaluation_hash == recorded.record.canonical_hash
        assert admission.idempotent_replay is False

    def test_same_day_replay_writes_nothing_and_is_byte_identical(self) -> None:
        """Requirement 3."""

        cycle = _Cycle()
        root = _buy_and_hold()
        _, _, first = cycle.admit(root)
        _, _, second = cycle.admit(_buy_and_hold(NOW + timedelta(hours=5)))
        assert second.idempotent_replay is True
        assert second.transition == first.transition
        assert second.version == first.version
        assert len(cycle.provenance.transitions_for_strategy(strategy_id=root.strategy_id)) == 1

    def test_later_day_admission_is_a_new_transition_to_the_same_artifact(self) -> None:
        cycle = _Cycle()
        root = _buy_and_hold()
        _, _, day_one = cycle.admit(root, as_of=NOW)
        _, _, day_two = cycle.admit(root, as_of=LATER)
        assert day_one.transition.transition_id != day_two.transition.transition_id
        assert day_one.transition.to_ref == day_two.transition.to_ref
        assert len(cycle.provenance.versions_for_strategy(strategy_id=root.strategy_id)) == 2

    def test_transition_cannot_bind_an_evaluation_of_another_artifact(self) -> None:
        """Requirements 1/4: A was evaluated; B (same id/version) cannot be
        promoted on A's evaluation, even though names and versions match."""

        cycle = _Cycle()
        version_a = _buy_and_hold()
        version_b = _edited(version_a, display_name="edited later")
        recorded_a, evaluation_a = cycle.evaluate(version_a)
        cycle.provenance.record_version(
            record=build_strategy_version_record(
                definition=version_b, registry=CATALOG, parent=None
            )
        )
        promoted_b = next_version(
            version_b, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        with pytest.raises(StrategyVersionProvenanceError, match="EVALUATED_ARTIFACT_MISMATCH"):
            RecordStrategyVersionTransition(cycle.provenance).execute(
                kind=ADMISSION,
                from_definition=version_b,
                to_definition=promoted_b,
                evaluation_source=INTERNAL,
                evaluation_id=recorded_a.record.evaluation_id,
                evaluation=evaluation_a,
                registry=CATALOG,
            )
        assert cycle.provenance.transitions_for_strategy(strategy_id=version_a.strategy_id) == ()

    def test_transition_requires_a_retained_subject(self) -> None:
        """Requirement 8: an evaluation that was never bound cannot justify."""

        cycle = _Cycle()
        root = _buy_and_hold()
        evaluation = _evaluate(root)
        promoted = promote_to_paper_eligible(root, evaluation=evaluation, promoted_at=NOW)
        with pytest.raises(StrategyVersionProvenanceResearchError, match="SUBJECT_UNRECORDED"):
            RecordStrategyVersionTransition(cycle.provenance).execute(
                kind=ADMISSION,
                from_definition=root,
                to_definition=promoted,
                evaluation_source=INTERNAL,
                evaluation_id="internal_strategy_evaluation_never_recorded",
                evaluation=evaluation,
                registry=CATALOG,
            )

    def test_evaluation_result_must_be_of_the_from_version(self) -> None:
        cycle = _Cycle()
        root = _buy_and_hold()
        recorded, _ = cycle.evaluate(root)
        promoted = promote_to_paper_eligible(root, evaluation=_evaluate(root), promoted_at=NOW)
        from_record = cycle.provenance.version(ref=StrategyVersionRef.of(root, registry=CATALOG))
        subject = cycle.provenance.subject_for(
            evaluation_source=INTERNAL, evaluation_id=recorded.record.evaluation_id
        )
        assert from_record is not None and subject is not None
        with pytest.raises(StrategyVersionProvenanceError, match="EVALUATION_RESULT_MISMATCH"):
            build_strategy_version_transition(
                kind=ADMISSION,
                from_version=from_record,
                to_version=build_strategy_version_record(
                    definition=promoted, registry=CATALOG, parent=root
                ),
                subject=subject,
                evaluation=_evaluate(promoted),
            )

    def test_rehydrate_detects_a_forged_transition_id(self) -> None:
        cycle = _Cycle()
        _, _, admission = cycle.admit(_buy_and_hold())
        transition = admission.transition
        with pytest.raises(StrategyVersionProvenanceError, match="TRANSITION_HASH_MISMATCH"):
            rehydrate_strategy_version_transition(
                kind=transition.kind,
                from_ref=transition.from_ref,
                to_ref=transition.to_ref,
                evaluation_source=transition.evaluation_source,
                evaluation_id=transition.evaluation_id,
                evaluation_hash=transition.evaluation_hash,
                gate_policy_version=transition.gate_policy_version,
                transition_id="strategy_version_transition_forged",
                canonical_hash=transition.canonical_hash,
            )

    def test_historical_rows_unchanged_after_later_revision(self) -> None:
        """Requirement 7: once the catalog later defines B, everything bound
        to A stays exactly as retained."""

        cycle = _Cycle()
        version_a = _buy_and_hold()
        _, _, admission_a = cycle.admit(version_a, as_of=NOW)
        snapshot = (
            cycle.provenance.versions_for_strategy(strategy_id=version_a.strategy_id),
            cycle.provenance.transitions_for_strategy(strategy_id=version_a.strategy_id),
        )
        version_b = _edited(version_a, display_name="edited later")
        _, _, admission_b = cycle.admit(version_b, as_of=LATER)
        versions = cycle.provenance.versions_for_strategy(strategy_id=version_a.strategy_id)
        transitions = cycle.provenance.transitions_for_strategy(strategy_id=version_a.strategy_id)
        assert set(snapshot[0]) <= set(versions)
        assert set(snapshot[1]) <= set(transitions)
        assert cycle.provenance.transition(transition_id=admission_a.transition.transition_id) == (
            admission_a.transition
        )
        assert admission_b.transition.from_ref.definition_hash == version_b.canonical_hash


class TestTrialProvenance:
    def test_trial_traces_back_to_the_originating_evaluated_artifact(self) -> None:
        """Requirement 6: trial -> transition -> evaluation -> v1 artifact."""

        cycle = _Cycle()
        root = _buy_and_hold()
        recorded, promoted, admission = cycle.admit(root)
        trial = _trial(promoted)
        opened = RecordProvenancedTrialOpened(cycle.provenance).execute(
            trial=trial, transition_id=admission.transition.transition_id
        )
        assert opened.idempotent_replay is False
        assert cycle.trials.get(trial_id=trial.trial_id) == trial
        bound = cycle.provenance.trial_provenance(trial_id=trial.trial_id)
        assert bound is not None
        transition = cycle.provenance.transition(transition_id=bound.transition_id)
        assert transition is not None
        assert bound.version_ref.definition_hash == promoted.canonical_hash
        assert transition.from_ref.definition_hash == root.canonical_hash
        assert transition.evaluation_id == recorded.record.evaluation_id
        assert (
            classify_trial_provenance(trial=trial, provenance=bound) is TrialProvenanceClass.BOUND
        )
        replay = RecordProvenancedTrialOpened(cycle.provenance).execute(
            trial=trial, transition_id=admission.transition.transition_id
        )
        assert replay.idempotent_replay is True
        assert replay.provenance == opened.provenance

    def test_trial_version_must_equal_the_admitted_version(self) -> None:
        cycle = _Cycle()
        root = _buy_and_hold()
        _, _, admission = cycle.admit(root)
        wrong = _trial(root)  # a v1 trial cannot bind to a v1 -> v2 admission
        with pytest.raises(StrategyVersionProvenanceError, match="VERSION_MISMATCH"):
            build_strategy_paper_trial_provenance(trial=wrong, transition=admission.transition)

    def test_legacy_trial_is_never_bound_and_is_explicitly_classified(self) -> None:
        """Requirement 9: the generation-1 production trial predates
        provenance; it stays readable and is labelled, never back-filled."""

        cycle = _Cycle()
        root = _buy_and_hold()
        _, promoted, admission = cycle.admit(root)
        legacy = _trial(promoted, legacy_adopted=True)
        with pytest.raises(StrategyVersionProvenanceError, match="LEGACY_NEVER_BOUND"):
            build_strategy_paper_trial_provenance(trial=legacy, transition=admission.transition)
        assert (
            classify_trial_provenance(trial=legacy, provenance=None)
            is TrialProvenanceClass.PREDATES_STRATEGY_PROVENANCE
        )
        report = strategy_version_provenance_report(
            provenance=cycle.provenance, strategy_id=root.strategy_id, trials=(legacy,)
        )
        trials = report["trials"]
        assert isinstance(trials, list)
        assert trials[0]["provenance_class"] == "PREDATES_STRATEGY_PROVENANCE"
        assert trials[0]["justifying_evaluation_id"] is None
        assert report["warnings"] == []

    def test_unbound_non_legacy_trial_is_reported_missing_not_guessed(self) -> None:
        """Requirement 8."""

        root = _buy_and_hold()
        promoted = next_version(
            root, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        trial = _trial(promoted)
        provenance = InMemoryStrategyVersionProvenanceRepository()
        assert (
            classify_trial_provenance(trial=trial, provenance=None)
            is TrialProvenanceClass.PROVENANCE_MISSING
        )
        report = strategy_version_provenance_report(
            provenance=provenance, strategy_id=root.strategy_id, trials=(trial,)
        )
        assert report["warnings"] == [f"TRIAL_PROVENANCE_MISSING:{trial.trial_id}"]

    def test_mismatched_binding_raises_instead_of_classifying(self) -> None:
        cycle = _Cycle()
        root = _buy_and_hold()
        _, promoted, admission = cycle.admit(root)
        first = _trial(promoted, generation=1)
        second = _trial(promoted, generation=2)
        bound = build_strategy_paper_trial_provenance(trial=first, transition=admission.transition)
        with pytest.raises(StrategyVersionProvenanceError, match="PROVENANCE_MISMATCH"):
            classify_trial_provenance(trial=second, provenance=bound)
        tampered = dataclasses.replace(first, canonical_hash="0" * 64)
        with pytest.raises(StrategyVersionProvenanceError, match="PROVENANCE_MISMATCH"):
            classify_trial_provenance(trial=tampered, provenance=bound)

    def test_opening_requires_a_retained_transition(self) -> None:
        cycle = _Cycle()
        root = _buy_and_hold()
        promoted = next_version(
            root, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        with pytest.raises(StrategyVersionProvenanceResearchError, match="TRANSITION_UNRECORDED"):
            RecordProvenancedTrialOpened(cycle.provenance).execute(
                trial=_trial(promoted), transition_id="strategy_version_transition_missing"
            )
        assert (
            cycle.trials.for_slot(
                strategy_id=root.strategy_id, strategy_version=2, instrument_id="BTC-USD"
            )
            == ()
        )


class TestInMemoryReferentialRules:
    def test_child_version_requires_retained_parent(self) -> None:
        root = _buy_and_hold()
        promoted = next_version(
            root, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        provenance = InMemoryStrategyVersionProvenanceRepository()
        with pytest.raises(StrategyVersionProvenanceResearchError, match="PARENT_NOT_RETAINED"):
            provenance.record_version(
                record=build_strategy_version_record(
                    definition=promoted, registry=CATALOG, parent=root
                )
            )

    def test_subject_identity_conflict_fails_closed(self) -> None:
        cycle = _Cycle()
        root = _buy_and_hold()
        recorded, _ = cycle.evaluate(root)
        edited = _edited(root, display_name="edited later")
        cycle.provenance.record_version(
            record=build_strategy_version_record(definition=edited, registry=CATALOG, parent=None)
        )
        with pytest.raises(StrategyVersionProvenanceResearchError, match="IDENTITY_CONFLICT"):
            cycle.provenance.record_subject(
                subject=build_strategy_evaluation_subject(
                    evaluation_source=INTERNAL,
                    evaluation_id=recorded.record.evaluation_id,
                    evaluation_hash=recorded.record.canonical_hash,
                    evaluated_strategy_id=recorded.record.strategy_id,
                    evaluated_strategy_version=1,
                    definition=edited,
                    registry=CATALOG,
                )
            )
