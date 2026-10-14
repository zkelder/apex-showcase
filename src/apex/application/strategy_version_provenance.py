"""Strategy Version Provenance v1: repository contract and write services.

See ``domain.strategy_version_provenance`` for the model and
``docs/STRATEGY_VERSION_PROVENANCE_V1.md`` for the reconstruction behind it.

Every service here writes append-only, idempotent rows keyed by
deterministic ids. Replaying the same logical event returns
``idempotent_replay=True`` and writes nothing. A different payload under the
same identity raises ``StrategyVersionProvenanceResearchError`` instead of
overwriting. Nothing here reads a mutable "current strategy". Callers pass
the exact definitions and evaluation records they actually used.

Authority ``NONE``. No service here can create an Opportunity, Order,
Position, PAPER authorization, provider request, or LIVE authority, and none
feeds ``domain.strategy_evaluation_history.current_lifecycle_state``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from apex.domain.strategy_definition import StrategyDefinition
from apex.domain.strategy_evaluation import StrategyEvaluationResult
from apex.domain.strategy_paper_trial_lifecycle import StrategyPaperTrialRecord
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyEvaluationSubject,
    StrategyPaperTrialProvenance,
    StrategyVersionRecord,
    StrategyVersionRef,
    StrategyVersionRegistry,
    StrategyVersionTransition,
    StrategyVersionTransitionKind,
    TrialProvenanceClass,
    build_strategy_evaluation_subject,
    build_strategy_paper_trial_provenance,
    build_strategy_version_record,
    build_strategy_version_transition,
    classify_trial_provenance,
)


class StrategyVersionProvenanceResearchError(ValueError):
    """A provenance write or lookup could not be completed. Fails closed."""


class StrategyVersionProvenanceRepository(Protocol):
    def record_version(self, *, record: StrategyVersionRecord) -> bool:
        """Insert or replay. True means an identical row already existed."""
        ...

    def record_subject(self, *, subject: StrategyEvaluationSubject) -> bool: ...

    def record_transition(self, *, transition: StrategyVersionTransition) -> bool: ...

    def version(self, *, ref: StrategyVersionRef) -> StrategyVersionRecord | None: ...

    def subject_for(
        self, *, evaluation_source: StrategyEvaluationSource, evaluation_id: str
    ) -> StrategyEvaluationSubject | None: ...

    def transition(self, *, transition_id: str) -> StrategyVersionTransition | None: ...

    def trial_provenance(self, *, trial_id: str) -> StrategyPaperTrialProvenance | None: ...

    def versions_for_strategy(self, *, strategy_id: str) -> tuple[StrategyVersionRecord, ...]: ...

    def subjects_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyEvaluationSubject, ...]: ...

    def transitions_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyVersionTransition, ...]: ...

    def trial_provenance_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyPaperTrialProvenance, ...]: ...


class ProvenancedTrialRepository(StrategyVersionProvenanceRepository, Protocol):
    """The only contract that can open a new trial row. Deliberately separate
    from ``StrategyVersionProvenanceRepository``, so promotion services, which
    depend only on the narrower contract, hold no trial-writing capability."""

    def record_trial_with_provenance(
        self, *, trial: StrategyPaperTrialRecord, provenance: StrategyPaperTrialProvenance
    ) -> bool:
        """Insert the trial row and its provenance row atomically, or replay."""
        ...


class _TrialWriter(Protocol):
    def register(self, *, record: StrategyPaperTrialRecord) -> bool: ...


class InMemoryStrategyVersionProvenanceRepository:
    """Reference/test implementation. Enforces the same referential rules
    the migration enforces with foreign keys, so application tests exercise
    the same fail-closed paths."""

    def __init__(self, trials: _TrialWriter | None = None) -> None:
        self._versions: dict[tuple[str, str, int, str], StrategyVersionRecord] = {}
        self._subjects: dict[tuple[str, str], StrategyEvaluationSubject] = {}
        self._transitions: dict[str, StrategyVersionTransition] = {}
        self._trials: dict[str, StrategyPaperTrialProvenance] = {}
        self._trial_writer = trials

    def record_version(self, *, record: StrategyVersionRecord) -> bool:
        if record.parent_ref is not None and record.parent_ref.as_tuple() not in self._versions:
            raise StrategyVersionProvenanceResearchError("STRATEGY_VERSION_PARENT_NOT_RETAINED")
        existing = self._versions.get(record.ref.as_tuple())
        if existing is not None:
            if existing.canonical_hash != record.canonical_hash:
                raise StrategyVersionProvenanceResearchError("STRATEGY_VERSION_IDENTITY_CONFLICT")
            return True
        self._versions[record.ref.as_tuple()] = record
        return False

    def record_subject(self, *, subject: StrategyEvaluationSubject) -> bool:
        if subject.subject.as_tuple() not in self._versions:
            raise StrategyVersionProvenanceResearchError("STRATEGY_EVALUATION_SUBJECT_NOT_RETAINED")
        key = (subject.evaluation_source.value, subject.evaluation_id)
        existing = self._subjects.get(key)
        if existing is not None:
            if existing.canonical_hash != subject.canonical_hash:
                raise StrategyVersionProvenanceResearchError(
                    "STRATEGY_EVALUATION_SUBJECT_IDENTITY_CONFLICT"
                )
            return True
        self._subjects[key] = subject
        return False

    def record_transition(self, *, transition: StrategyVersionTransition) -> bool:
        subject = self._subjects.get((transition.evaluation_source.value, transition.evaluation_id))
        if subject is None or subject.subject != transition.from_ref:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_VERSION_TRANSITION_EVALUATED_ARTIFACT_NOT_RETAINED"
            )
        to_record = self._versions.get(transition.to_ref.as_tuple())
        if to_record is None or to_record.parent_ref != transition.from_ref:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_VERSION_TRANSITION_TARGET_NOT_RETAINED"
            )
        existing = self._transitions.get(transition.transition_id)
        if existing is not None:
            if existing.canonical_hash != transition.canonical_hash:
                raise StrategyVersionProvenanceResearchError(
                    "STRATEGY_VERSION_TRANSITION_IDENTITY_CONFLICT"
                )
            return True
        self._transitions[transition.transition_id] = transition
        return False

    def record_trial_with_provenance(
        self, *, trial: StrategyPaperTrialRecord, provenance: StrategyPaperTrialProvenance
    ) -> bool:
        transition = self._transitions.get(provenance.transition_id)
        if transition is None or transition.to_ref != provenance.version_ref:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_PAPER_TRIAL_PROVENANCE_TRANSITION_NOT_RETAINED"
            )
        existing = self._trials.get(trial.trial_id)
        if existing is not None:
            if existing.canonical_hash != provenance.canonical_hash:
                raise StrategyVersionProvenanceResearchError(
                    "STRATEGY_PAPER_TRIAL_PROVENANCE_IDENTITY_CONFLICT"
                )
            return True
        if self._trial_writer is not None:
            self._trial_writer.register(record=trial)
        self._trials[trial.trial_id] = provenance
        return False

    def version(self, *, ref: StrategyVersionRef) -> StrategyVersionRecord | None:
        return self._versions.get(ref.as_tuple())

    def subject_for(
        self, *, evaluation_source: StrategyEvaluationSource, evaluation_id: str
    ) -> StrategyEvaluationSubject | None:
        return self._subjects.get((evaluation_source.value, evaluation_id))

    def transition(self, *, transition_id: str) -> StrategyVersionTransition | None:
        return self._transitions.get(transition_id)

    def trial_provenance(self, *, trial_id: str) -> StrategyPaperTrialProvenance | None:
        return self._trials.get(trial_id)

    def versions_for_strategy(self, *, strategy_id: str) -> tuple[StrategyVersionRecord, ...]:
        return tuple(
            sorted(
                (item for item in self._versions.values() if item.ref.strategy_id == strategy_id),
                key=lambda item: item.ref,
            )
        )

    def subjects_for_strategy(self, *, strategy_id: str) -> tuple[StrategyEvaluationSubject, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._subjects.values()
                    if item.subject.strategy_id == strategy_id
                ),
                key=lambda item: (item.evaluation_source.value, item.evaluation_id),
            )
        )

    def transitions_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyVersionTransition, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._transitions.values()
                    if item.from_ref.strategy_id == strategy_id
                ),
                key=lambda item: item.transition_id,
            )
        )

    def trial_provenance_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyPaperTrialProvenance, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._trials.values()
                    if item.version_ref.strategy_id == strategy_id
                ),
                key=lambda item: item.trial_id,
            )
        )


@dataclass(frozen=True, slots=True)
class RecordEvaluationSubjectResult:
    version: StrategyVersionRecord
    subject: StrategyEvaluationSubject
    idempotent_replay: bool


class RecordEvaluationSubject:
    """Retain the exact evaluated *root* artifact and bind one retained
    evaluation record to it. Callers invoke this immediately after
    persisting the evaluation, with the same in-memory definition the
    evaluation ran on."""

    def __init__(self, provenance: StrategyVersionProvenanceRepository) -> None:
        self._provenance = provenance

    def execute(
        self,
        *,
        definition: StrategyDefinition,
        registry: StrategyVersionRegistry,
        evaluation_source: StrategyEvaluationSource,
        evaluation_id: str,
        evaluation_hash: str,
        evaluated_strategy_id: str,
        evaluated_strategy_version: int,
        parent: StrategyDefinition | None = None,
    ) -> RecordEvaluationSubjectResult:
        version = build_strategy_version_record(
            definition=definition, registry=registry, parent=parent
        )
        subject = build_strategy_evaluation_subject(
            evaluation_source=evaluation_source,
            evaluation_id=evaluation_id,
            evaluation_hash=evaluation_hash,
            evaluated_strategy_id=evaluated_strategy_id,
            evaluated_strategy_version=evaluated_strategy_version,
            definition=definition,
            registry=registry,
        )
        version_replay = self._provenance.record_version(record=version)
        subject_replay = self._provenance.record_subject(subject=subject)
        return RecordEvaluationSubjectResult(
            version=version, subject=subject, idempotent_replay=version_replay and subject_replay
        )


@dataclass(frozen=True, slots=True)
class RecordStrategyVersionTransitionResult:
    version: StrategyVersionRecord
    transition: StrategyVersionTransition
    idempotent_replay: bool


class RecordStrategyVersionTransition:
    """Retain a promoted or admitted artifact and the transition justifying
    it. ``subject`` must be the *retained* subject of the justifying
    evaluation. It is re-read from the repository and never trusted from
    the caller alone, so a transition can bind only to evaluated artifacts
    that actually exist."""

    def __init__(self, provenance: StrategyVersionProvenanceRepository) -> None:
        self._provenance = provenance

    def execute(
        self,
        *,
        kind: StrategyVersionTransitionKind,
        from_definition: StrategyDefinition,
        to_definition: StrategyDefinition,
        evaluation_source: StrategyEvaluationSource,
        evaluation_id: str,
        evaluation: StrategyEvaluationResult,
        registry: StrategyVersionRegistry,
    ) -> RecordStrategyVersionTransitionResult:
        subject = self._provenance.subject_for(
            evaluation_source=evaluation_source, evaluation_id=evaluation_id
        )
        if subject is None:
            raise StrategyVersionProvenanceResearchError("STRATEGY_EVALUATION_SUBJECT_UNRECORDED")
        from_record = self._provenance.version(
            ref=StrategyVersionRef.of(from_definition, registry=registry)
        )
        if from_record is None:
            raise StrategyVersionProvenanceResearchError("STRATEGY_VERSION_FROM_NOT_RETAINED")
        to_record = build_strategy_version_record(
            definition=to_definition, registry=registry, parent=from_definition
        )
        transition = build_strategy_version_transition(
            kind=kind,
            from_version=from_record,
            to_version=to_record,
            subject=subject,
            evaluation=evaluation,
        )
        version_replay = self._provenance.record_version(record=to_record)
        transition_replay = self._provenance.record_transition(transition=transition)
        return RecordStrategyVersionTransitionResult(
            version=to_record,
            transition=transition,
            idempotent_replay=version_replay and transition_replay,
        )


@dataclass(frozen=True, slots=True)
class RecordProvenancedTrialOpenedResult:
    trial: StrategyPaperTrialRecord
    provenance: StrategyPaperTrialProvenance
    idempotent_replay: bool


class RecordProvenancedTrialOpened:
    """Open a *new* (non-legacy) PAPER trial and its provenance binding
    together. The database also rejects, at commit, any non-legacy trial row
    without its provenance (migration 0115), so this is the only write path
    for new trials."""

    def __init__(self, provenance: ProvenancedTrialRepository) -> None:
        self._provenance = provenance

    def execute(
        self, *, trial: StrategyPaperTrialRecord, transition_id: str
    ) -> RecordProvenancedTrialOpenedResult:
        transition = self._provenance.transition(transition_id=transition_id)
        if transition is None:
            raise StrategyVersionProvenanceResearchError("STRATEGY_VERSION_TRANSITION_UNRECORDED")
        provenance = build_strategy_paper_trial_provenance(trial=trial, transition=transition)
        replay = self._provenance.record_trial_with_provenance(trial=trial, provenance=provenance)
        return RecordProvenancedTrialOpenedResult(
            trial=trial, provenance=provenance, idempotent_replay=replay
        )


def strategy_version_provenance_report(
    *,
    provenance: StrategyVersionProvenanceRepository,
    strategy_id: str,
    trials: tuple[StrategyPaperTrialRecord, ...] = (),
) -> dict[str, object]:
    """Read-only lineage for one strategy_id: every retained artifact (by
    registry), every evaluation subject, every transition, and every PAPER
    trial with its provenance classification. More than one hash for the
    same (registry, strategy_id, version) is reported as a warning, not an
    error: each artifact stays distinct and bound, and nothing is merged."""

    versions = provenance.versions_for_strategy(strategy_id=strategy_id)
    by_slot: dict[tuple[str, int], set[str]] = {}
    for item in versions:
        by_slot.setdefault((item.ref.registry.value, item.ref.version), set()).add(
            item.ref.definition_hash
        )
    warnings = sorted(
        f"MULTIPLE_ARTIFACTS_FOR_VERSION:{registry}:{version}:{len(hashes)}"
        for (registry, version), hashes in by_slot.items()
        if len(hashes) > 1
    )
    trial_rows = []
    for trial in trials:
        bound = provenance.trial_provenance(trial_id=trial.trial_id)
        classification = classify_trial_provenance(trial=trial, provenance=bound)
        if classification is TrialProvenanceClass.PROVENANCE_MISSING:
            warnings.append(f"TRIAL_PROVENANCE_MISSING:{trial.trial_id}")
        transition = (
            None if bound is None else provenance.transition(transition_id=bound.transition_id)
        )
        trial_rows.append(
            {
                "trial_id": trial.trial_id,
                "strategy_version": trial.strategy_version,
                "generation": trial.generation,
                "legacy_adopted": trial.legacy_adopted,
                "provenance_class": classification.value,
                "transition_id": None if bound is None else bound.transition_id,
                "definition_hash": None if bound is None else bound.version_ref.definition_hash,
                "justifying_evaluation_id": (
                    None if transition is None else transition.evaluation_id
                ),
                "evaluated_definition_hash": (
                    None if transition is None else transition.from_ref.definition_hash
                ),
            }
        )
    return {
        "strategy_id": strategy_id,
        "authority": "NONE",
        "versions": [
            {
                "registry": item.ref.registry.value,
                "version": item.ref.version,
                "definition_hash": item.ref.definition_hash,
                "lifecycle_state": item.lifecycle_state.value,
                "parent_version": None if item.parent_ref is None else item.parent_ref.version,
                "parent_definition_hash": (
                    None if item.parent_ref is None else item.parent_ref.definition_hash
                ),
            }
            for item in versions
        ],
        "evaluation_subjects": [
            {
                "evaluation_source": item.evaluation_source.value,
                "evaluation_id": item.evaluation_id,
                "registry": item.subject.registry.value,
                "version": item.subject.version,
                "definition_hash": item.subject.definition_hash,
            }
            for item in provenance.subjects_for_strategy(strategy_id=strategy_id)
        ],
        "transitions": [
            {
                "transition_id": item.transition_id,
                "kind": item.kind.value,
                "registry": item.from_ref.registry.value,
                "from_version": item.from_ref.version,
                "from_definition_hash": item.from_ref.definition_hash,
                "to_version": item.to_ref.version,
                "to_definition_hash": item.to_ref.definition_hash,
                "evaluation_source": item.evaluation_source.value,
                "evaluation_id": item.evaluation_id,
                "gate_policy_version": item.gate_policy_version,
            }
            for item in provenance.transitions_for_strategy(strategy_id=strategy_id)
        ],
        "trials": trial_rows,
        "warnings": warnings,
    }
