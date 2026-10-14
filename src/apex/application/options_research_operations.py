"""Idempotent orchestration for Fresh Options Evidence Operations and
Structure Research Baseline v1.

Every service here is additive research-evidence orchestration only: no
Order, Strategy, Shared Opportunity, PAPER order, or LIVE authority is ever
created, and every persisted fact keeps ``authority = "NONE"``.

Deliberately thin, and deliberately adapter-free (this module imports no
provider-specific type, matching ``application.options_quote_intelligence``/
``application.options_structures``): the actual per-underlying Alpaca
capture loop -- raw entry parsing, network I/O, pagination -- stays in the
CLI composition root exactly where the existing ``options-chain-capture``
command already does it (``scripts/dev.py``), reusing the identical already-
reviewed ``AlpacaOptionsChainCaptureClient``/``observation_from_alpaca_
entry``/``CreateOptionsChainSnapshot``/``BuildStructuresForUnderlying``
building blocks. What lives here is everything that is genuinely provider-
agnostic: due-state resolution, checkpoint recording, and distinct-session
history/readiness aggregation over already-retained evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from apex.domain.options_quote_intelligence import (
    OptionsChainSnapshot,
    OptionsQuoteProviderCapabilitySnapshot,
)
from apex.domain.options_research_operations import (
    MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
    OptionsCaptureDueDecision,
    OptionsCaptureWindowPolicy,
    OptionsForwardEvidenceCheckpoint,
    OptionsHistoryReadiness,
    determine_options_capture_due_state,
    evaluate_options_history_readiness,
    session_id_for_captured_at,
)
from apex.domain.securities import SecuritiesCalendarSession


class OptionsResearchOperationsApplicationError(RuntimeError):
    pass


class OptionsQuoteCapabilityReader(Protocol):
    """The one narrow slice of ``OptionsQuoteIntelligenceRepository`` this
    module needs -- accepted as a Protocol rather than the full repository
    so a due-state check never accidentally gains write access to quote
    evidence it has no business touching."""

    def latest_capability(self, provider: str) -> OptionsQuoteProviderCapabilitySnapshot | None: ...


class OptionsSnapshotHistoryReader(Protocol):
    """The one narrow slice of ``OptionsQuoteIntelligenceRepository`` this
    module needs to compute distinct-session history readiness."""

    def snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsChainSnapshot, ...]: ...


class OptionsResearchOperationsRepository(Protocol):
    def save_checkpoint(
        self, checkpoint: OptionsForwardEvidenceCheckpoint
    ) -> OptionsForwardEvidenceCheckpoint: ...

    def checkpoint_by_id(self, checkpoint_id: str) -> OptionsForwardEvidenceCheckpoint | None: ...

    def checkpoints_for_session(
        self, *, plan_id: str, session_id: str
    ) -> tuple[OptionsForwardEvidenceCheckpoint, ...]: ...

    def checkpoints_for_plan(
        self, plan_id: str
    ) -> tuple[OptionsForwardEvidenceCheckpoint, ...]: ...

    def latest_checkpoint_for_session(
        self, *, plan_id: str, session_id: str
    ) -> OptionsForwardEvidenceCheckpoint | None: ...

    def latest_checkpoint(self, plan_id: str) -> OptionsForwardEvidenceCheckpoint | None: ...

    def completed_session_ids(self, plan_id: str) -> frozenset[str]: ...


class RecordOptionsForwardEvidenceCheckpoint:
    """Content-addressed: recording the identical checkpoint twice (e.g. a
    same-session replay that captured no further underlyings) returns the
    already-retained row rather than a duplicate."""

    def __init__(self, repository: OptionsResearchOperationsRepository) -> None:
        self._repository = repository

    def execute(
        self, *, checkpoint: OptionsForwardEvidenceCheckpoint
    ) -> OptionsForwardEvidenceCheckpoint:
        existing = self._repository.checkpoint_by_id(checkpoint.checkpoint_id)
        if existing is not None:
            return existing
        return self._repository.save_checkpoint(checkpoint)


class DetermineOptionsResearchCaptureDueState:
    """Resolves the two retained facts ``determine_options_capture_due_
    state`` needs beyond the calendar and wall clock -- genuine capability
    presence and the set of sessions already fully captured -- from the
    already-existing quote-intelligence and new checkpoint repositories,
    so no caller has to assemble those itself."""

    def __init__(
        self,
        *,
        checkpoint_repository: OptionsResearchOperationsRepository,
        capability_reader: OptionsQuoteCapabilityReader,
    ) -> None:
        self._checkpoint_repository = checkpoint_repository
        self._capability_reader = capability_reader

    def execute(
        self,
        *,
        plan_id: str,
        provider: str,
        sessions: tuple[SecuritiesCalendarSession, ...],
        policy: OptionsCaptureWindowPolicy,
        now: datetime,
    ) -> OptionsCaptureDueDecision:
        latest_capability = self._capability_reader.latest_capability(provider)
        capability_supported = bool(
            latest_capability is not None and latest_capability.credentials_available
        )
        captured_session_ids = self._checkpoint_repository.completed_session_ids(plan_id)
        return determine_options_capture_due_state(
            sessions=sessions,
            policy=policy,
            now=now,
            capability_supported=capability_supported,
            captured_session_ids=captured_session_ids,
        )


class ComputeOptionsHistoryReadiness:
    """Genuine distinct-session forward-history readiness for one
    underlying, derived from every already-retained chain snapshot whose
    ``captured_at`` genuinely falls inside a real session's due window --
    never a same-session replay, a duplicate, or a stale-reproduction
    snapshot captured outside every window (see
    ``domain.options_research_operations.session_id_for_captured_at``)."""

    def __init__(self, *, quote_repository: OptionsSnapshotHistoryReader) -> None:
        self._quote_repository = quote_repository

    def execute(
        self,
        *,
        underlying_canonical_security_id: str,
        sessions: tuple[SecuritiesCalendarSession, ...],
        policy: OptionsCaptureWindowPolicy,
        minimum_required_sessions: int = MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
    ) -> OptionsHistoryReadiness:
        snapshots = self._quote_repository.snapshots_for_underlying(
            underlying_canonical_security_id
        )
        session_ids = tuple(
            session_id
            for snapshot in snapshots
            if (
                session_id := session_id_for_captured_at(
                    captured_at=snapshot.captured_at,
                    sessions=sessions,
                    policy=policy,
                )
            )
            is not None
        )
        return evaluate_options_history_readiness(
            underlying_canonical_security_id=underlying_canonical_security_id,
            session_ids=session_ids,
            minimum_required_sessions=minimum_required_sessions,
        )


class InMemoryOptionsResearchOperationsRepository:
    def __init__(self) -> None:
        self._checkpoints: dict[str, OptionsForwardEvidenceCheckpoint] = {}

    def save_checkpoint(
        self, checkpoint: OptionsForwardEvidenceCheckpoint
    ) -> OptionsForwardEvidenceCheckpoint:
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def checkpoint_by_id(self, checkpoint_id: str) -> OptionsForwardEvidenceCheckpoint | None:
        return self._checkpoints.get(checkpoint_id)

    def checkpoints_for_session(
        self, *, plan_id: str, session_id: str
    ) -> tuple[OptionsForwardEvidenceCheckpoint, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._checkpoints.values()
                    if item.plan_id == plan_id and item.session_id == session_id
                ),
                key=lambda item: item.created_at,
            )
        )

    def checkpoints_for_plan(self, plan_id: str) -> tuple[OptionsForwardEvidenceCheckpoint, ...]:
        return tuple(
            sorted(
                (item for item in self._checkpoints.values() if item.plan_id == plan_id),
                key=lambda item: item.created_at,
            )
        )

    def latest_checkpoint_for_session(
        self, *, plan_id: str, session_id: str
    ) -> OptionsForwardEvidenceCheckpoint | None:
        matches = self.checkpoints_for_session(plan_id=plan_id, session_id=session_id)
        return matches[-1] if matches else None

    def latest_checkpoint(self, plan_id: str) -> OptionsForwardEvidenceCheckpoint | None:
        matches = self.checkpoints_for_plan(plan_id)
        return matches[-1] if matches else None

    def completed_session_ids(self, plan_id: str) -> frozenset[str]:
        return frozenset(
            item.session_id
            for item in self._checkpoints.values()
            if item.plan_id == plan_id
            and item.underlyings_captured_count == item.underlyings_requested_count
            and item.underlyings_requested_count > 0
        )
