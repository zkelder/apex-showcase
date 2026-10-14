"""Application-layer repository contracts, in-memory reference
implementations, and orchestration services for Strategy Evaluation
History & Promotion Lineage v1 -- see ``domain.strategy_evaluation_history``'s
own module docstring for the full rationale and the "internal" vocabulary
caveat.

Two deliberately small, explicit services, mirroring the exact "one action,
one purpose" idiom ``application.external_strategy_research`` already uses:
``RecordInternalStrategyEvaluation`` (persist one already-computed
evaluation result idempotently -- never evaluates, never promotes) and
``PromoteInternalStrategy`` (the one explicit, caller-invoked action that
can durably record a promotion-lineage event for a recurring-catalog
strategy -- never called automatically).

Neither service imports ``application.strategy_paper_trial``/
``application.crypto_paper`` -- the only code in this codebase that can
place a PAPER order (structurally verified by
``tests/test_strategy_evaluation_history_application.py``). ``adapters.
operating_cycle_tasks._execute_strategy_paper_trial`` (the live recurring
task) calls ``RecordInternalStrategyEvaluation`` directly to retain its own
real per-cycle evaluation result, but never calls ``PromoteInternalStrategy``
-- durable promotion for the recurring catalog is deliberately an
operator-invoked action only, exactly mirroring how ``PromoteExternalStrategy``
is never called by ``RegisterExternalStrategy``/``EvaluateExternalStrategy``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.application.strategy_version_provenance import (
    RecordStrategyVersionTransition,
    StrategyVersionProvenanceRepository,
    StrategyVersionProvenanceResearchError,
)
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks
from apex.domain.strategy_definition import StrategyDefinition
from apex.domain.strategy_evaluation import (
    StrategyEvaluationError,
    StrategyEvaluationResult,
    promote_to_paper_eligible,
)
from apex.domain.strategy_evaluation_history import (
    STRATEGY_EVALUATION_HISTORY_WARNING,
    InternalStrategyEvaluationRecord,
    InternalStrategyPromotionRecord,
    build_internal_strategy_evaluation_record,
    build_internal_strategy_promotion_record,
    current_lifecycle_state,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionProvenanceError,
    StrategyVersionRef,
    StrategyVersionRegistry,
    StrategyVersionTransitionKind,
)

__all__ = [
    "STRATEGY_EVALUATION_HISTORY_WARNING",
    "InMemoryInternalStrategyEvaluationRepository",
    "InMemoryInternalStrategyPromotionRepository",
    "InternalStrategyEvaluationRepository",
    "InternalStrategyPromotionRepository",
    "PromoteInternalStrategy",
    "PromoteInternalStrategyResult",
    "RecordInternalStrategyEvaluation",
    "RecordInternalStrategyEvaluationResult",
    "StrategyEvaluationHistoryResearchError",
    "internal_strategy_evaluation_history",
]


class StrategyEvaluationHistoryResearchError(ValueError):
    """A record/promote action could not honestly proceed (unknown
    evaluation, an evaluation/strategy pairing that does not belong
    together, a strategy_id not a member of the recurring catalog, ...).
    Fails closed."""


class InternalStrategyEvaluationRepository(Protocol):
    def register(self, *, record: InternalStrategyEvaluationRecord) -> bool:
        """Persist one immutable evaluation record idempotently. Returns
        ``True`` on idempotent replay of an already-identical retained row
        (same ``evaluation_id``, same ``canonical_hash``), ``False`` on a
        genuine new write. Raises ``StrategyEvaluationHistoryResearchError``
        if the same ``evaluation_id`` already exists with a different
        ``canonical_hash`` -- structurally should not happen given
        deterministic identity, but fails closed rather than silently
        overwriting if it ever does."""

    def get(self, *, evaluation_id: str) -> InternalStrategyEvaluationRecord | None: ...

    def for_strategy(self, *, strategy_id: str) -> tuple[InternalStrategyEvaluationRecord, ...]:
        """Every retained evaluation for one strategy_id, most-recently-
        ``evaluated_at`` first."""

    def latest_for_strategy(self, *, strategy_id: str) -> InternalStrategyEvaluationRecord | None:
        """The most-recently-``evaluated_at`` retained evaluation for one
        strategy_id. ``None`` if none is retained."""


class InternalStrategyPromotionRepository(Protocol):
    def register(self, *, record: InternalStrategyPromotionRecord) -> bool:
        """Same idempotent-replay/identity-conflict contract as
        ``InternalStrategyEvaluationRepository.register``, keyed by
        ``promotion_id``."""

    def get(self, *, promotion_id: str) -> InternalStrategyPromotionRecord | None: ...

    def for_strategy(self, *, strategy_id: str) -> tuple[InternalStrategyPromotionRecord, ...]:
        """Every retained promotion event for one strategy_id, in no
        particular guaranteed order -- callers needing "latest" should use
        ``domain.strategy_evaluation_history.current_lifecycle_state`` or
        sort explicitly by ``promoted_at``."""


class InMemoryInternalStrategyEvaluationRepository:
    """Reference/test implementation."""

    def __init__(self) -> None:
        self._rows: dict[str, InternalStrategyEvaluationRecord] = {}

    def register(self, *, record: InternalStrategyEvaluationRecord) -> bool:
        existing = self._rows.get(record.evaluation_id)
        if existing is not None:
            if existing.canonical_hash != record.canonical_hash:
                raise StrategyEvaluationHistoryResearchError(
                    "INTERNAL_STRATEGY_EVALUATION_IDENTITY_CONFLICT"
                )
            return True
        self._rows[record.evaluation_id] = record
        return False

    def get(self, *, evaluation_id: str) -> InternalStrategyEvaluationRecord | None:
        return self._rows.get(evaluation_id)

    def for_strategy(self, *, strategy_id: str) -> tuple[InternalStrategyEvaluationRecord, ...]:
        return tuple(
            sorted(
                (item for item in self._rows.values() if item.strategy_id == strategy_id),
                key=lambda item: (item.evaluated_at, item.evaluation_id),
                reverse=True,
            )
        )

    def latest_for_strategy(self, *, strategy_id: str) -> InternalStrategyEvaluationRecord | None:
        rows = self.for_strategy(strategy_id=strategy_id)
        return rows[0] if rows else None


class InMemoryInternalStrategyPromotionRepository:
    """Reference/test implementation."""

    def __init__(self) -> None:
        self._rows: dict[str, InternalStrategyPromotionRecord] = {}

    def register(self, *, record: InternalStrategyPromotionRecord) -> bool:
        existing = self._rows.get(record.promotion_id)
        if existing is not None:
            if existing.canonical_hash != record.canonical_hash:
                raise StrategyEvaluationHistoryResearchError(
                    "INTERNAL_STRATEGY_PROMOTION_IDENTITY_CONFLICT"
                )
            return True
        self._rows[record.promotion_id] = record
        return False

    def get(self, *, promotion_id: str) -> InternalStrategyPromotionRecord | None:
        return self._rows.get(promotion_id)

    def for_strategy(self, *, strategy_id: str) -> tuple[InternalStrategyPromotionRecord, ...]:
        return tuple(item for item in self._rows.values() if item.strategy_id == strategy_id)


@dataclass(frozen=True, slots=True)
class RecordInternalStrategyEvaluationResult:
    record: InternalStrategyEvaluationRecord
    idempotent_replay: bool


class RecordInternalStrategyEvaluation:
    """Build + idempotently persist one ``InternalStrategyEvaluationRecord``
    from a ``StrategyDefinition`` and the ``StrategyEvaluationResult`` the
    caller ALREADY computed elsewhere. Never runs a backtest, never
    evaluates, never promotes -- see ``domain.strategy_evaluation_history.
    build_internal_strategy_evaluation_record``'s own docstring (Part 7:
    the durable record must reflect the exact result the caller actually
    used, not a second recomputation)."""

    def __init__(self, evaluations: InternalStrategyEvaluationRepository) -> None:
        self._evaluations = evaluations

    def execute(
        self,
        *,
        definition: StrategyDefinition,
        candidate: StrategyEvaluationResult,
        cycle_id: str,
        as_of: datetime,
        capture_id: str,
        evaluated_at: datetime,
    ) -> RecordInternalStrategyEvaluationResult:
        record = build_internal_strategy_evaluation_record(
            definition=definition,
            candidate=candidate,
            cycle_id=cycle_id,
            as_of=as_of,
            capture_id=capture_id,
            evaluated_at=evaluated_at,
        )
        idempotent_replay = self._evaluations.register(record=record)
        return RecordInternalStrategyEvaluationResult(
            record=record, idempotent_replay=idempotent_replay
        )


def _reconstruct_catalog_definition(
    strategy_id: str, *, created_at: datetime
) -> StrategyDefinition | None:
    """The recurring catalog's own definitions are never persisted (see
    module docstring) -- they are reconstructed byte-identically from
    reviewed source code on demand. ``created_at`` never affects identity
    (``StrategyDefinition.canonical_hash`` excludes it), so this is a safe,
    deterministic lookup, not a fabrication. Returns ``None`` if
    ``strategy_id`` is not a member of today's catalog -- Part 7's "do not
    invent additional strategy candidates" boundary, enforced structurally
    here rather than merely by convention."""

    for item in canonical_strategy_benchmarks(created_at=created_at):
        if item.strategy_id == strategy_id:
            return item
    return None


@dataclass(frozen=True, slots=True)
class PromoteInternalStrategyResult:
    promotion: InternalStrategyPromotionRecord
    idempotent_replay: bool


class PromoteInternalStrategy:
    """The one explicit, caller-invoked action that can durably record a
    promotion-lineage event for one of the recurring/internal catalog's own
    candidates. Reuses the real, unmodified ``strategy_evaluation.promote_
    to_paper_eligible`` gate -- never a looser or duplicated copy of it.
    Never invoked by ``RecordInternalStrategyEvaluation``, and never invoked
    automatically by the recurring cycle (``adapters.operating_cycle_tasks.
    _execute_strategy_paper_trial`` never imports this class) -- see Part 9/
    22 of the milestone this module implements: durable evaluation history
    must never itself trigger PAPER action or lifecycle mutation.

    Because the recurring catalog's own ``StrategyDefinition`` is
    reconstructed fresh (never durably versioned -- see module docstring),
    ``promote_to_paper_eligible``'s own "already promoted" guard can never
    fire here (the reconstructed definition is always ``RESEARCH_ONLY``).
    This means promoting the SAME strategy_id against multiple, distinct,
    later evaluation_ids over time is legitimate and expected -- each is
    its own honest re-affirmation event; ``domain.strategy_evaluation_
    history.current_lifecycle_state`` always resolves to the LATEST one.
    """

    def __init__(
        self,
        *,
        evaluations: InternalStrategyEvaluationRepository,
        promotions: InternalStrategyPromotionRepository,
        provenance: StrategyVersionProvenanceRepository,
    ) -> None:
        self._evaluations = evaluations
        self._promotions = promotions
        self._provenance = provenance

    def execute(
        self, *, strategy_id: str, evaluation_id: str, promoted_at: datetime, actor: str
    ) -> PromoteInternalStrategyResult:
        record = self._evaluations.get(evaluation_id=evaluation_id)
        if record is None:
            raise StrategyEvaluationHistoryResearchError("INTERNAL_STRATEGY_EVALUATION_NOT_FOUND")
        if record.strategy_id != strategy_id:
            raise StrategyEvaluationHistoryResearchError(
                "INTERNAL_STRATEGY_EVALUATION_DOES_NOT_BELONG_TO_THIS_STRATEGY"
            )
        # Strategy Version Provenance v1 (gap G4): the catalog is rebuilt from
        # today's code, but promotion is only valid for the exact artifact the
        # evaluation was bound to when it ran. An evaluation with no retained
        # subject (every evaluation before 0115) or a catalog that no longer
        # builds that artifact fails closed. Nothing is guessed from name,
        # family, or version number.
        subject = self._provenance.subject_for(
            evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
            evaluation_id=evaluation_id,
        )
        if subject is None:
            raise StrategyEvaluationHistoryResearchError(
                "INTERNAL_STRATEGY_EVALUATION_SUBJECT_UNRECORDED"
            )
        definition = _reconstruct_catalog_definition(strategy_id, created_at=promoted_at)
        if definition is None:
            raise StrategyEvaluationHistoryResearchError(
                "INTERNAL_STRATEGY_NOT_A_RECOGNIZED_CATALOG_CANDIDATE"
            )
        if subject.subject != StrategyVersionRef.of(
            definition, registry=StrategyVersionRegistry.CATALOG_CODE
        ):
            raise StrategyEvaluationHistoryResearchError(
                "INTERNAL_STRATEGY_CURRENT_DEFINITION_DIFFERS_FROM_EVALUATED_ARTIFACT"
            )
        try:
            promoted = promote_to_paper_eligible(
                definition, evaluation=record.candidate, promoted_at=promoted_at
            )
        except StrategyEvaluationError as error:
            raise StrategyEvaluationHistoryResearchError(
                f"INTERNAL_STRATEGY_PROMOTION_REJECTED:{error}"
            ) from error
        try:
            RecordStrategyVersionTransition(self._provenance).execute(
                kind=StrategyVersionTransitionKind.INTERNAL_OPERATOR_PROMOTION,
                from_definition=definition,
                to_definition=promoted,
                evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
                evaluation_id=evaluation_id,
                evaluation=record.candidate,
                registry=StrategyVersionRegistry.CATALOG_CODE,
            )
        except (StrategyVersionProvenanceError, StrategyVersionProvenanceResearchError) as error:
            raise StrategyEvaluationHistoryResearchError(
                f"INTERNAL_STRATEGY_PROMOTION_PROVENANCE_REJECTED:{error}"
            ) from error
        promotion = build_internal_strategy_promotion_record(
            strategy_id=strategy_id,
            strategy_version=record.strategy_version,
            evaluation_id=evaluation_id,
            lifecycle_state_before=definition.lifecycle_state,
            lifecycle_state_after=promoted.lifecycle_state,
            promoted_at=promoted_at,
            actor=actor,
        )
        idempotent_replay = self._promotions.register(record=promotion)
        return PromoteInternalStrategyResult(
            promotion=promotion, idempotent_replay=idempotent_replay
        )


def internal_strategy_evaluation_history(
    *,
    evaluations: InternalStrategyEvaluationRepository,
    promotions: InternalStrategyPromotionRepository,
    strategy_id: str,
) -> dict[str, object]:
    """Full retained lineage for one recurring-catalog strategy: every
    retained evaluation (most-recent-first) and every retained explicit
    promotion event, plus the derived current operational lifecycle state
    (Part 14). Read-only, descriptive; never mutates anything."""

    strategy_evaluations = evaluations.for_strategy(strategy_id=strategy_id)
    strategy_promotions = promotions.for_strategy(strategy_id=strategy_id)
    # Latest-by-promoted_at, computed here rather than trusted from
    # repository ordering: InternalStrategyPromotionRepository.for_strategy
    # makes no ordering guarantee (Protocol docstring), so this is the one
    # place "latest" is resolved -- the exact same max-by-(promoted_at,
    # promotion_id) rule domain.strategy_evaluation_history.current_
    # lifecycle_state already uses, reused rather than re-derived.
    latest_promotion = (
        max(strategy_promotions, key=lambda item: (item.promoted_at, item.promotion_id))
        if strategy_promotions
        else None
    )
    return {
        "strategy_id": strategy_id,
        "evaluations": strategy_evaluations,
        "latest_evaluation": strategy_evaluations[0] if strategy_evaluations else None,
        "promotions": strategy_promotions,
        "latest_promotion": latest_promotion,
        "current_lifecycle_state": current_lifecycle_state(strategy_promotions).value,
        "authority": "NONE",
        "warning": STRATEGY_EVALUATION_HISTORY_WARNING,
    }


def internal_strategy_evaluation_registry(
    *,
    evaluations: InternalStrategyEvaluationRepository,
    promotions: InternalStrategyPromotionRepository,
    strategy_ids: Sequence[str],
) -> dict[str, dict[str, object]]:
    """``internal_strategy_evaluation_history`` for every given strategy_id
    in one call -- the Operator Console's own console-load shape, since the
    recurring catalog is always this exact small, fixed, bounded set (today
    four candidates; see module docstring). Each lookup below stays a
    small, strategy_id-scoped query (never a full-table scan), so this
    bounded fan-out is not the N+1-over-an-unbounded-set shape the
    Operator Console Home/Followups Performance v1 precedent had to fix."""

    return {
        strategy_id: internal_strategy_evaluation_history(
            evaluations=evaluations, promotions=promotions, strategy_id=strategy_id
        )
        for strategy_id in strategy_ids
    }
