"""Application-layer repository contracts, in-memory reference
implementations, and orchestration services for Repeatable Strategy PAPER
Evidence v1 -- see ``domain.strategy_paper_trial_lifecycle``'s own module
docstring for the full rationale.

Two deliberately small, explicit services, mirroring the exact "one
action, one purpose" idiom ``application.strategy_evaluation_history``
already uses for its own sibling evaluation/promotion pair:
``RecordStrategyPaperTrialOpened`` (persist one already-built trial record
idempotently -- never decides whether to open one) and
``RecordStrategyPaperTrialClosed`` (persist one already-built closure
record idempotently -- never decides whether a trial is due to close).
Every DECISION (is a trial due to close, is a retrial eligible, what
generation comes next) is made by the pure functions in ``domain.
strategy_paper_trial_lifecycle``; every MECHANICAL PAPER effect (the
actual SELL that closes a position) is made by ``application.strategy_
paper_trial.ExecuteStrategyPaperTrial.close_position``. Neither of those
two things lives here -- this module is narrowly the durable-bookkeeping
layer, exactly mirroring how ``application.strategy_evaluation_history``
relates to ``domain.strategy_evaluation``/``application.strategy_paper_
trial``.

Neither service imports ``application.strategy_paper_trial``/
``application.crypto_paper`` -- the only code in this codebase that can
place a PAPER order. ``adapters.operating_cycle_tasks._execute_strategy_
paper_trial`` (the live recurring task) calls both services directly to
retain its own real trial-open/trial-close events; nothing in this module
ever decides to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from apex.domain.strategy_paper_trial_lifecycle import (
    STRATEGY_PAPER_TRIAL_LIFECYCLE_WARNING,
    StrategyPaperTrialClosureRecord,
    StrategyPaperTrialRecord,
)

__all__ = [
    "STRATEGY_PAPER_TRIAL_LIFECYCLE_WARNING",
    "InMemoryStrategyPaperTrialClosureRepository",
    "InMemoryStrategyPaperTrialRepository",
    "RecordStrategyPaperTrialClosed",
    "RecordStrategyPaperTrialClosedResult",
    "RecordStrategyPaperTrialOpened",
    "RecordStrategyPaperTrialOpenedResult",
    "StrategyPaperTrialClosureRepository",
    "StrategyPaperTrialLifecycleResearchError",
    "StrategyPaperTrialRepository",
    "strategy_paper_trial_lineage",
]


class StrategyPaperTrialLifecycleResearchError(ValueError):
    """A record action could not honestly proceed (an identity conflict,
    a closure referencing an unknown trial, a second closure for an
    already-terminal trial). Fails closed."""


class StrategyPaperTrialRepository(Protocol):
    def register(self, *, record: StrategyPaperTrialRecord) -> bool:
        """Persist one immutable trial-open record idempotently. Returns
        ``True`` on idempotent replay of an already-identical retained row
        (same ``trial_id``, same ``canonical_hash``), ``False`` on a
        genuine new write. Raises ``StrategyPaperTrialLifecycleResearchError``
        if the same ``trial_id`` already exists with a different
        ``canonical_hash`` -- structurally should not happen given
        deterministic identity, but fails closed rather than silently
        overwriting if it ever does."""

    def get(self, *, trial_id: str) -> StrategyPaperTrialRecord | None: ...

    def for_slot(
        self, *, strategy_id: str, strategy_version: int, instrument_id: str
    ) -> tuple[StrategyPaperTrialRecord, ...]:
        """Every retained trial generation for one slot, ordered by
        ``generation`` ascending (oldest/first generation first)."""


class StrategyPaperTrialClosureRepository(Protocol):
    def register(self, *, record: StrategyPaperTrialClosureRecord) -> bool:
        """Same idempotent-replay/identity-conflict contract as
        ``StrategyPaperTrialRepository.register``, keyed by ``closure_id``.
        A second, genuinely DIFFERENT closure attempt for a ``trial_id``
        that already has one raises -- see ``RecordStrategyPaperTrialClosed``
        for the explicit pre-check that produces the clearer error before
        this repository-level guard would ever be reached."""

    def get(self, *, closure_id: str) -> StrategyPaperTrialClosureRecord | None: ...

    def for_trial(self, *, trial_id: str) -> StrategyPaperTrialClosureRecord | None:
        """The one closure for a given ``trial_id``, if any -- at most one
        can ever exist (DB-enforced ``UNIQUE`` constraint in Postgres; the
        in-memory reference implementation enforces the identical
        invariant explicitly)."""

    def for_strategy(self, *, strategy_id: str) -> tuple[StrategyPaperTrialClosureRecord, ...]:
        """Every retained closure for one strategy_id (across every
        instrument/generation), in no particular guaranteed order."""


class InMemoryStrategyPaperTrialRepository:
    """Reference/test implementation."""

    def __init__(self) -> None:
        self._rows: dict[str, StrategyPaperTrialRecord] = {}

    def register(self, *, record: StrategyPaperTrialRecord) -> bool:
        existing = self._rows.get(record.trial_id)
        if existing is not None:
            if existing.canonical_hash != record.canonical_hash:
                raise StrategyPaperTrialLifecycleResearchError(
                    "STRATEGY_PAPER_TRIAL_IDENTITY_CONFLICT"
                )
            return True
        self._rows[record.trial_id] = record
        return False

    def get(self, *, trial_id: str) -> StrategyPaperTrialRecord | None:
        return self._rows.get(trial_id)

    def for_slot(
        self, *, strategy_id: str, strategy_version: int, instrument_id: str
    ) -> tuple[StrategyPaperTrialRecord, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._rows.values()
                    if item.strategy_id == strategy_id
                    and item.strategy_version == strategy_version
                    and item.instrument_id == instrument_id
                ),
                key=lambda item: item.generation,
            )
        )


class InMemoryStrategyPaperTrialClosureRepository:
    """Reference/test implementation."""

    def __init__(self) -> None:
        self._rows: dict[str, StrategyPaperTrialClosureRecord] = {}
        self._by_trial: dict[str, str] = {}

    def register(self, *, record: StrategyPaperTrialClosureRecord) -> bool:
        existing = self._rows.get(record.closure_id)
        if existing is not None:
            if existing.canonical_hash != record.canonical_hash:
                raise StrategyPaperTrialLifecycleResearchError(
                    "STRATEGY_PAPER_TRIAL_CLOSURE_IDENTITY_CONFLICT"
                )
            return True
        other_closure_id = self._by_trial.get(record.trial_id)
        if other_closure_id is not None and other_closure_id != record.closure_id:
            raise StrategyPaperTrialLifecycleResearchError("STRATEGY_PAPER_TRIAL_ALREADY_CLOSED")
        self._rows[record.closure_id] = record
        self._by_trial[record.trial_id] = record.closure_id
        return False

    def get(self, *, closure_id: str) -> StrategyPaperTrialClosureRecord | None:
        return self._rows.get(closure_id)

    def for_trial(self, *, trial_id: str) -> StrategyPaperTrialClosureRecord | None:
        closure_id = self._by_trial.get(trial_id)
        return None if closure_id is None else self._rows.get(closure_id)

    def for_strategy(self, *, strategy_id: str) -> tuple[StrategyPaperTrialClosureRecord, ...]:
        return tuple(item for item in self._rows.values() if item.strategy_id == strategy_id)

    def all_closures(self) -> tuple[StrategyPaperTrialClosureRecord, ...]:
        return tuple(sorted(self._rows.values(), key=lambda item: (item.closed_at, item.trial_id)))


@dataclass(frozen=True, slots=True)
class RecordStrategyPaperTrialOpenedResult:
    record: StrategyPaperTrialRecord
    idempotent_replay: bool


class RecordStrategyPaperTrialOpened:
    """Idempotently persist one already-built ``StrategyPaperTrialRecord``.
    Never decides whether a new trial generation should begin -- see
    ``domain.strategy_paper_trial_lifecycle.evaluate_retrial_eligibility``/
    ``next_trial_generation``/``adopt_legacy_trial`` for that; this class
    only durably retains the caller's already-made decision, matching
    ``application.strategy_evaluation_history.RecordInternalStrategyEvaluation``'s
    identical "build elsewhere, persist here" split."""

    def __init__(self, trials: StrategyPaperTrialRepository) -> None:
        self._trials = trials

    def execute(self, *, record: StrategyPaperTrialRecord) -> RecordStrategyPaperTrialOpenedResult:
        idempotent_replay = self._trials.register(record=record)
        return RecordStrategyPaperTrialOpenedResult(
            record=record, idempotent_replay=idempotent_replay
        )


@dataclass(frozen=True, slots=True)
class RecordStrategyPaperTrialClosedResult:
    record: StrategyPaperTrialClosureRecord
    idempotent_replay: bool


class RecordStrategyPaperTrialClosed:
    """Idempotently persist one already-built ``StrategyPaperTrialClosureRecord``,
    after two explicit, honestly-named checks that produce a clear reason
    before any repository-level identity-conflict guard would otherwise
    fire: the referenced trial must actually exist, and (unless this is a
    pure replay of the SAME closure) it must not already be closed. Never
    decides whether a trial is due to close -- see ``domain.strategy_paper_
    trial_lifecycle.evaluate_trial_horizon`` for that."""

    def __init__(
        self, *, trials: StrategyPaperTrialRepository, closures: StrategyPaperTrialClosureRepository
    ) -> None:
        self._trials = trials
        self._closures = closures

    def execute(
        self, *, record: StrategyPaperTrialClosureRecord
    ) -> RecordStrategyPaperTrialClosedResult:
        trial = self._trials.get(trial_id=record.trial_id)
        if trial is None:
            raise StrategyPaperTrialLifecycleResearchError("STRATEGY_PAPER_TRIAL_NOT_FOUND")
        existing = self._closures.for_trial(trial_id=record.trial_id)
        if existing is not None and existing.closure_id != record.closure_id:
            raise StrategyPaperTrialLifecycleResearchError("STRATEGY_PAPER_TRIAL_ALREADY_CLOSED")
        idempotent_replay = self._closures.register(record=record)
        return RecordStrategyPaperTrialClosedResult(
            record=record, idempotent_replay=idempotent_replay
        )


def strategy_paper_trial_lineage(
    *,
    trials: StrategyPaperTrialRepository,
    closures: StrategyPaperTrialClosureRepository,
    strategy_id: str,
    strategy_version: int,
    instrument_id: str,
) -> dict[str, object]:
    """Full retained trial lineage for one slot: every generation
    (oldest-first), each paired with its closure if one exists, plus the
    derived "which one is active right now" (Part 10: operator/read-model
    visibility). Read-only, descriptive; never mutates anything. Mirrors
    ``application.strategy_evaluation_history.
    internal_strategy_evaluation_history``'s identical read-model shape."""

    slot_trials = trials.for_slot(
        strategy_id=strategy_id, strategy_version=strategy_version, instrument_id=instrument_id
    )
    generations: list[dict[str, object]] = []
    active_trial: StrategyPaperTrialRecord | None = None
    latest_closure: StrategyPaperTrialClosureRecord | None = None
    for trial in slot_trials:
        closure = closures.for_trial(trial_id=trial.trial_id)
        generations.append({"trial": trial, "closure": closure})
        if closure is None:
            active_trial = trial
        else:
            latest_closure = closure
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "instrument_id": instrument_id,
        "generations": tuple(generations),
        "trial_count": len(slot_trials),
        "active_trial": active_trial,
        "latest_closure": latest_closure,
        "authority": "NONE",
        "warning": STRATEGY_PAPER_TRIAL_LIFECYCLE_WARNING,
    }
