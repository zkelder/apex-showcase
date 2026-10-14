"""Strategy Version Provenance v1 (Challenger Provenance v1).

One small canonical primitive, ``StrategyVersionRef``, plus three immutable
edges that bind Apex's rule-strategy lifecycle to the exact artifact at
every step:

    StrategyVersionRecord        the exact immutable artifact (registry,
                                 strategy_id, version, definition_hash) and
                                 its exact parent artifact
    StrategyEvaluationSubject    evaluation/recommendation  ->  the exact
                                 artifact that was evaluated
    StrategyVersionTransition    evaluated artifact -> promoted/admitted
                                 artifact, justified by that exact
                                 evaluation
    StrategyPaperTrialProvenance PAPER trial -> the exact transition (and so
                                 the exact admitted artifact) it traded

See ``docs/STRATEGY_VERSION_PROVENANCE_V1.md`` for the reconstruction this
was built from (gaps G1-G9).

Design rules:

* **The artifact is the triple plus its registry, never ``(strategy_id,
  version)``.** ``StrategyDefinition.strategy_id`` is shared by every
  version, and it is shared by *different* definitions whose descriptive
  fields differ (gap G2). It is even shared across governance registries:
  an operator-registered EXTERNAL buy-and-hold on BTC-USD has the catalog
  buy-and-hold's ``strategy_id`` (gap G9). ``definition_hash`` is the
  definition's own ``canonical_hash``, and ``registry`` records which
  governance boundary owns it.
* **Nothing here reads a mutable "current" definition.** Every builder takes
  the exact in-memory ``StrategyDefinition`` objects the caller actually
  evaluated or traded, and checks them against each other. The catalog is
  rebuilt from code every cycle, but a transition is valid only when that
  rebuilt definition hashes to the artifact the evaluation was bound to.
* **Deterministic identity, no wall clock.** Every id and ``canonical_hash``
  here is a pure function of evidence content. ``recorded_at`` is retained
  on rows for operators and never enters identity, so replay reproduces
  byte-identical provenance.
* **Fail closed.** A mismatch raises ``StrategyVersionProvenanceError``.
  Nothing is guessed, coerced, or resolved by family or name.
* **No fabricated history.** Rows are written prospectively only. A trial
  opened before this mechanism existed is *classified* from a fact already
  on its own immutable row (``legacy_adopted``). No provenance row is
  invented for it (see ``classify_trial_provenance``).

Authority ``NONE`` throughout. Nothing here creates an Opportunity, Order,
Position, PAPER authorization, provider request, or LIVE authority, and
nothing here feeds ``domain.strategy_evaluation_history.
current_lifecycle_state``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyLifecycleState,
    next_version,
)
from apex.domain.strategy_evaluation import StrategyEvaluationResult
from apex.domain.strategy_paper_trial_lifecycle import StrategyPaperTrialRecord
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id

STRATEGY_VERSION_PROVENANCE_WARNING = (
    "STRATEGY VERSION PROVENANCE -- IMMUTABLE LINEAGE EVIDENCE ONLY; "
    "NO CHAMPION, PROMOTION, PAPER, OR LIVE AUTHORITY"
)

STRATEGY_VERSION_PROVENANCE_POLICY_V1 = "STRATEGY_VERSION_PROVENANCE_POLICY_V1"

#: The gate every transition kind below actually ran. Retained per row so a
#: future gate change is visible in lineage instead of silently reinterpreted.
#: ``promote_to_paper_eligible`` / ``promote_to_active`` implement exactly
#: ``INTERNAL_STRATEGY_PROMOTION_POLICY_V1``'s thresholds (sample size >= 30,
#: net return > 0) for every registry, so one gate id covers all of them.
STRATEGY_PROMOTION_GATE_POLICY_V1 = "STRATEGY_PROMOTION_GATE_POLICY_V1"


class StrategyVersionProvenanceError(ValueError):
    """A provenance input failed an identity or lineage invariant. Fails closed."""


class StrategyVersionRegistry(StrEnum):
    """Which governance boundary owns a version. The same content registered
    in two registries is two distinct provenance artifacts (gap G9)."""

    CATALOG_CODE = "CATALOG_CODE"
    EXTERNAL_OPERATOR = "EXTERNAL_OPERATOR"


class StrategyEvaluationSource(StrEnum):
    INTERNAL_STRATEGY_EVALUATION = "INTERNAL_STRATEGY_EVALUATION"
    EXTERNAL_STRATEGY_EVALUATION = "EXTERNAL_STRATEGY_EVALUATION"


class StrategyVersionTransitionKind(StrEnum):
    #: The recurring cycle's per-day gate check that actually permitted PAPER
    #: trading (the in-memory ``promote_to_paper_eligible`` call). Deliberately
    #: not called a promotion: it never changes the durable operational
    #: lifecycle state (``internal_strategy_promotion``, migration 0110).
    CYCLE_GATE_ADMISSION = "CYCLE_GATE_ADMISSION"
    #: ``application.strategy_evaluation_history.PromoteInternalStrategy``.
    INTERNAL_OPERATOR_PROMOTION = "INTERNAL_OPERATOR_PROMOTION"
    #: ``application.external_strategy_research.PromoteExternalStrategy``.
    EXTERNAL_OPERATOR_PROMOTION = "EXTERNAL_OPERATOR_PROMOTION"


_TRANSITION_REGISTRY: dict[StrategyVersionTransitionKind, StrategyVersionRegistry] = {
    StrategyVersionTransitionKind.CYCLE_GATE_ADMISSION: StrategyVersionRegistry.CATALOG_CODE,
    StrategyVersionTransitionKind.INTERNAL_OPERATOR_PROMOTION: (
        StrategyVersionRegistry.CATALOG_CODE
    ),
    StrategyVersionTransitionKind.EXTERNAL_OPERATOR_PROMOTION: (
        StrategyVersionRegistry.EXTERNAL_OPERATOR
    ),
}

_EVALUATION_SOURCE_REGISTRY: dict[StrategyEvaluationSource, StrategyVersionRegistry] = {
    StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION: StrategyVersionRegistry.CATALOG_CODE,
    StrategyEvaluationSource.EXTERNAL_STRATEGY_EVALUATION: (
        StrategyVersionRegistry.EXTERNAL_OPERATOR
    ),
}


@dataclass(frozen=True, slots=True, order=True)
class StrategyVersionRef:
    """The canonical identity of one exact immutable strategy artifact."""

    registry: StrategyVersionRegistry
    strategy_id: str
    version: int
    definition_hash: str

    @classmethod
    def of(
        cls, definition: StrategyDefinition, *, registry: StrategyVersionRegistry
    ) -> StrategyVersionRef:
        return cls(
            registry=registry,
            strategy_id=definition.strategy_id,
            version=definition.version,
            definition_hash=definition.canonical_hash,
        )

    def as_tuple(self) -> tuple[str, str, int, str]:
        return (self.registry.value, self.strategy_id, self.version, self.definition_hash)


def definition_artifact_bytes(definition: StrategyDefinition) -> bytes:
    """The exact canonical bytes whose SHA-256 *is* ``definition.canonical_hash``.
    Persisting these bytes lets the database itself prove, with a CHECK
    constraint, that a stored artifact is the hashed artifact."""

    payload = canonical_json_bytes(definition.canonical_payload)
    if sha256(payload).hexdigest() != definition.canonical_hash:
        # StrategyDefinition.create computes canonical_hash exactly this way,
        # so this can only fire for a hand-built or corrupted instance.
        raise StrategyVersionProvenanceError("STRATEGY_DEFINITION_ARTIFACT_HASH_MISMATCH")
    return payload


def _require_derivation(parent: StrategyDefinition, child: StrategyDefinition) -> None:
    """``child`` must be exactly the version the reviewed lifecycle functions
    derive from ``parent``: same strategy, version + 1, and every descriptive
    field carried forward unchanged. Checked by rebuilding the successor and
    comparing hashes, never by trusting a field that names the parent."""

    if child.strategy_id != parent.strategy_id:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_PARENT_STRATEGY_MISMATCH")
    if child.version != parent.version + 1:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_PARENT_NOT_IMMEDIATE_PREDECESSOR")
    if child.lifecycle_state is StrategyLifecycleState.ACTIVE:
        expected_hash = StrategyDefinition.create(
            source_kind=parent.source_kind,
            display_name=parent.display_name,
            venue=parent.venue,
            instrument_ids=parent.instrument_ids,
            horizon_kind=parent.horizon_kind,
            rule_kind=parent.rule_kind,
            rule_parameters=parent.rule_parameters,
            evidence_requirements=parent.evidence_requirements,
            provenance=parent.provenance,
            created_at=child.created_at,
            version=parent.version + 1,
            parent_strategy_id=parent.strategy_id,
            lifecycle_state=StrategyLifecycleState.ACTIVE,
        ).canonical_hash
    else:
        expected_hash = next_version(
            parent, lifecycle_state=child.lifecycle_state, created_at=child.created_at
        ).canonical_hash
    if expected_hash != child.canonical_hash:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_NOT_DERIVED_FROM_PARENT")


@dataclass(frozen=True, slots=True)
class StrategyVersionRecord:
    ref: StrategyVersionRef
    lifecycle_state: StrategyLifecycleState
    parent_ref: StrategyVersionRef | None
    artifact: bytes
    canonical_hash: str
    authority: str = "NONE"


def build_strategy_version_record(
    *,
    definition: StrategyDefinition,
    registry: StrategyVersionRegistry,
    parent: StrategyDefinition | None,
) -> StrategyVersionRecord:
    """Pure constructor for one immutable version artifact. A root (version 1)
    has no parent. Every later version must be *derived* from the exact
    parent artifact supplied (see ``_require_derivation``)."""

    if (definition.version == 1) != (parent is None):
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_ROOT_IFF_VERSION_ONE_AND_NO_PARENT")
    if parent is not None:
        _require_derivation(parent, definition)
    ref = StrategyVersionRef.of(definition, registry=registry)
    parent_ref = None if parent is None else StrategyVersionRef.of(parent, registry=registry)
    return rehydrate_strategy_version_record(
        ref=ref,
        lifecycle_state=definition.lifecycle_state,
        parent_ref=parent_ref,
        artifact=definition_artifact_bytes(definition),
        canonical_hash=None,
    )


def rehydrate_strategy_version_record(
    *,
    ref: StrategyVersionRef,
    lifecycle_state: StrategyLifecycleState,
    parent_ref: StrategyVersionRef | None,
    artifact: bytes,
    canonical_hash: str | None,
) -> StrategyVersionRecord:
    """Build (``canonical_hash=None``) or verify a retained row. Checks that
    the artifact bytes hash to ``ref.definition_hash`` and that every
    identity column agrees with the artifact itself."""

    if sha256(artifact).hexdigest() != ref.definition_hash:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_ARTIFACT_HASH_MISMATCH")
    decoded = json.loads(artifact)
    if (
        decoded.get("strategy_id") != ref.strategy_id
        or decoded.get("version") != ref.version
        or decoded.get("lifecycle_state") != lifecycle_state.value
    ):
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_ARTIFACT_IDENTITY_MISMATCH")
    if parent_ref is not None and (
        parent_ref.registry is not ref.registry
        or parent_ref.strategy_id != ref.strategy_id
        or parent_ref.version != ref.version - 1
    ):
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_PARENT_REF_INVALID")
    computed = content_digest(
        (
            STRATEGY_VERSION_PROVENANCE_POLICY_V1,
            ref.as_tuple(),
            lifecycle_state.value,
            None if parent_ref is None else parent_ref.as_tuple(),
        )
    )
    if canonical_hash is not None and canonical_hash != computed:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_RECORD_HASH_MISMATCH")
    return StrategyVersionRecord(
        ref=ref,
        lifecycle_state=lifecycle_state,
        parent_ref=parent_ref,
        artifact=artifact,
        canonical_hash=computed,
    )


@dataclass(frozen=True, slots=True)
class StrategyEvaluationSubject:
    evaluation_source: StrategyEvaluationSource
    evaluation_id: str
    evaluation_hash: str
    subject: StrategyVersionRef
    canonical_hash: str
    authority: str = "NONE"


def build_strategy_evaluation_subject(
    *,
    evaluation_source: StrategyEvaluationSource,
    evaluation_id: str,
    evaluation_hash: str,
    evaluated_strategy_id: str,
    evaluated_strategy_version: int,
    definition: StrategyDefinition,
    registry: StrategyVersionRegistry,
) -> StrategyEvaluationSubject:
    """Bind one retained evaluation record (the recommendation) to the exact
    artifact that was evaluated. ``evaluated_strategy_id`` and
    ``evaluated_strategy_version`` are the values *on the evaluation record*.
    They must match ``definition``, and so they can never be resolved by
    family, name, or latest version."""

    if _EVALUATION_SOURCE_REGISTRY[evaluation_source] is not registry:
        raise StrategyVersionProvenanceError("STRATEGY_EVALUATION_SOURCE_REGISTRY_MISMATCH")
    if not evaluation_id.strip() or len(evaluation_hash) != 64:
        raise StrategyVersionProvenanceError("STRATEGY_EVALUATION_IDENTITY_INVALID")
    if (
        evaluated_strategy_id != definition.strategy_id
        or evaluated_strategy_version != definition.version
    ):
        raise StrategyVersionProvenanceError("STRATEGY_EVALUATION_SUBJECT_MISMATCH")
    return rehydrate_strategy_evaluation_subject(
        evaluation_source=evaluation_source,
        evaluation_id=evaluation_id,
        evaluation_hash=evaluation_hash,
        subject=StrategyVersionRef.of(definition, registry=registry),
        canonical_hash=None,
    )


def rehydrate_strategy_evaluation_subject(
    *,
    evaluation_source: StrategyEvaluationSource,
    evaluation_id: str,
    evaluation_hash: str,
    subject: StrategyVersionRef,
    canonical_hash: str | None,
) -> StrategyEvaluationSubject:
    if _EVALUATION_SOURCE_REGISTRY[evaluation_source] is not subject.registry:
        raise StrategyVersionProvenanceError("STRATEGY_EVALUATION_SOURCE_REGISTRY_MISMATCH")
    computed = content_digest(
        (
            STRATEGY_VERSION_PROVENANCE_POLICY_V1,
            evaluation_source.value,
            evaluation_id,
            evaluation_hash,
            subject.as_tuple(),
        )
    )
    if canonical_hash is not None and canonical_hash != computed:
        raise StrategyVersionProvenanceError("STRATEGY_EVALUATION_SUBJECT_HASH_MISMATCH")
    return StrategyEvaluationSubject(
        evaluation_source=evaluation_source,
        evaluation_id=evaluation_id,
        evaluation_hash=evaluation_hash,
        subject=subject,
        canonical_hash=computed,
    )


@dataclass(frozen=True, slots=True)
class StrategyVersionTransition:
    transition_id: str
    kind: StrategyVersionTransitionKind
    from_ref: StrategyVersionRef
    to_ref: StrategyVersionRef
    evaluation_source: StrategyEvaluationSource
    evaluation_id: str
    evaluation_hash: str
    gate_policy_version: str
    canonical_hash: str
    authority: str = "NONE"


def build_strategy_version_transition(
    *,
    kind: StrategyVersionTransitionKind,
    from_version: StrategyVersionRecord,
    to_version: StrategyVersionRecord,
    subject: StrategyEvaluationSubject,
    evaluation: StrategyEvaluationResult,
) -> StrategyVersionTransition:
    """One promotion or admission: ``from_version`` was evaluated by
    ``subject``'s evaluation, and that evaluation justified ``to_version``.
    ``evaluation`` is the exact result the gate ran on, and it must be the
    result of the subject's own evaluation (checked by version, so an
    evaluation of a different version can never justify this transition)."""

    registry = _TRANSITION_REGISTRY[kind]
    if from_version.ref.registry is not registry or to_version.ref.registry is not registry:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_TRANSITION_REGISTRY_MISMATCH")
    if to_version.parent_ref != from_version.ref:
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_TRANSITION_PARENT_MISMATCH")
    if subject.subject != from_version.ref:
        # G3/G4: the justifying evaluation must be of the exact artifact being
        # promoted -- same registry, id, version, and definition hash.
        raise StrategyVersionProvenanceError(
            "STRATEGY_VERSION_TRANSITION_EVALUATED_ARTIFACT_MISMATCH"
        )
    if (
        evaluation.strategy_id != from_version.ref.strategy_id
        or evaluation.strategy_version != from_version.ref.version
    ):
        raise StrategyVersionProvenanceError(
            "STRATEGY_VERSION_TRANSITION_EVALUATION_RESULT_MISMATCH"
        )
    return rehydrate_strategy_version_transition(
        kind=kind,
        from_ref=from_version.ref,
        to_ref=to_version.ref,
        evaluation_source=subject.evaluation_source,
        evaluation_id=subject.evaluation_id,
        evaluation_hash=subject.evaluation_hash,
        gate_policy_version=STRATEGY_PROMOTION_GATE_POLICY_V1,
        transition_id=None,
        canonical_hash=None,
    )


def rehydrate_strategy_version_transition(
    *,
    kind: StrategyVersionTransitionKind,
    from_ref: StrategyVersionRef,
    to_ref: StrategyVersionRef,
    evaluation_source: StrategyEvaluationSource,
    evaluation_id: str,
    evaluation_hash: str,
    gate_policy_version: str,
    transition_id: str | None,
    canonical_hash: str | None,
) -> StrategyVersionTransition:
    registry = _TRANSITION_REGISTRY[kind]
    if (
        from_ref.registry is not registry
        or to_ref.registry is not registry
        or _EVALUATION_SOURCE_REGISTRY[evaluation_source] is not registry
        or from_ref.strategy_id != to_ref.strategy_id
        or to_ref.version != from_ref.version + 1
    ):
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_TRANSITION_SHAPE_INVALID")
    identity = (
        STRATEGY_VERSION_PROVENANCE_POLICY_V1,
        kind.value,
        from_ref.as_tuple(),
        to_ref.as_tuple(),
        evaluation_source.value,
        evaluation_id,
        gate_policy_version,
    )
    computed_id = deterministic_id("strategy_version_transition", identity)
    computed_hash = content_digest((*identity, evaluation_hash))
    if (transition_id is not None and transition_id != computed_id) or (
        canonical_hash is not None and canonical_hash != computed_hash
    ):
        raise StrategyVersionProvenanceError("STRATEGY_VERSION_TRANSITION_HASH_MISMATCH")
    return StrategyVersionTransition(
        transition_id=computed_id,
        kind=kind,
        from_ref=from_ref,
        to_ref=to_ref,
        evaluation_source=evaluation_source,
        evaluation_id=evaluation_id,
        evaluation_hash=evaluation_hash,
        gate_policy_version=gate_policy_version,
        canonical_hash=computed_hash,
    )


@dataclass(frozen=True, slots=True)
class StrategyPaperTrialProvenance:
    trial_id: str
    trial_hash: str
    transition_id: str
    version_ref: StrategyVersionRef
    canonical_hash: str
    authority: str = "NONE"


def build_strategy_paper_trial_provenance(
    *, trial: StrategyPaperTrialRecord, transition: StrategyVersionTransition
) -> StrategyPaperTrialProvenance:
    """Bind a *new* PAPER trial to the exact transition that admitted the
    artifact it trades. A legacy-adopted trial is never bound. It predates
    this mechanism, and binding it to any transition would fabricate history
    (see ``classify_trial_provenance``)."""

    if trial.legacy_adopted:
        raise StrategyVersionProvenanceError("STRATEGY_PAPER_TRIAL_LEGACY_NEVER_BOUND")
    to_ref = transition.to_ref
    if to_ref.registry is not StrategyVersionRegistry.CATALOG_CODE:
        raise StrategyVersionProvenanceError("STRATEGY_PAPER_TRIAL_REGISTRY_UNSUPPORTED")
    if trial.strategy_id != to_ref.strategy_id or trial.strategy_version != to_ref.version:
        raise StrategyVersionProvenanceError("STRATEGY_PAPER_TRIAL_VERSION_MISMATCH")
    return rehydrate_strategy_paper_trial_provenance(
        trial_id=trial.trial_id,
        trial_hash=trial.canonical_hash,
        transition_id=transition.transition_id,
        version_ref=to_ref,
        canonical_hash=None,
    )


def rehydrate_strategy_paper_trial_provenance(
    *,
    trial_id: str,
    trial_hash: str,
    transition_id: str,
    version_ref: StrategyVersionRef,
    canonical_hash: str | None,
) -> StrategyPaperTrialProvenance:
    computed = content_digest(
        (
            STRATEGY_VERSION_PROVENANCE_POLICY_V1,
            trial_id,
            trial_hash,
            transition_id,
            version_ref.as_tuple(),
        )
    )
    if canonical_hash is not None and canonical_hash != computed:
        raise StrategyVersionProvenanceError("STRATEGY_PAPER_TRIAL_PROVENANCE_HASH_MISMATCH")
    return StrategyPaperTrialProvenance(
        trial_id=trial_id,
        trial_hash=trial_hash,
        transition_id=transition_id,
        version_ref=version_ref,
        canonical_hash=computed,
    )


class TrialProvenanceClass(StrEnum):
    #: A retained binding exists: trial -> transition -> evaluation -> artifact.
    BOUND = "BOUND"
    #: The trial row itself says ``legacy_adopted=True``. It was opened before
    #: evaluation history and provenance existed. Nothing is reconstructed.
    PREDATES_STRATEGY_PROVENANCE = "PREDATES_STRATEGY_PROVENANCE"
    #: A non-legacy trial with no binding. It should be unreachable: the
    #: database rejects a non-legacy trial insert without its provenance in
    #: the same transaction. Reported loudly, never guessed.
    PROVENANCE_MISSING = "PROVENANCE_MISSING"


def classify_trial_provenance(
    *, trial: StrategyPaperTrialRecord, provenance: StrategyPaperTrialProvenance | None
) -> TrialProvenanceClass:
    if provenance is not None:
        if (
            provenance.trial_id != trial.trial_id
            or provenance.trial_hash != trial.canonical_hash
            or (
                provenance.version_ref.strategy_id,
                provenance.version_ref.version,
            )
            != (trial.strategy_id, trial.strategy_version)
        ):
            raise StrategyVersionProvenanceError("STRATEGY_PAPER_TRIAL_PROVENANCE_MISMATCH")
        return TrialProvenanceClass.BOUND
    if trial.legacy_adopted:
        return TrialProvenanceClass.PREDATES_STRATEGY_PROVENANCE
    return TrialProvenanceClass.PROVENANCE_MISSING
