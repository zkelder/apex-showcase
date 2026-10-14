"""Idempotent synchronization of the Counterfactual Decision Ledger from
already-retained origin decisions.

Each service here performs no provider I/O and creates no new investment
judgment -- it reads already-durable evidence (``SharedOpportunitySet``/
``EligibilityDecision`` or ``MandateEvaluation``) through a narrow read-only
source protocol, normalizes it with the pure builders in
``domain.counterfactual_ledger``, and persists the result idempotently.
Re-running either service over unchanged upstream evidence writes nothing
new (every row replays); re-running after new upstream evidence appears
(a new opportunity set, a new mandate evaluation) only ever adds rows, never
mutates or removes one.  There is deliberately no "last synced" cursor: at
today's evidence volume (hundreds of rows total), a full idempotent rescan
is simpler and strictly safer than cursor state that could itself drift; a
later milestone can add one without changing the ledger schema if volume
ever justifies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    CounterfactualLedgerError,
    build_decision_from_eligibility,
    build_decision_from_mandate_evaluation,
)
from apex.domain.investment_mandate import MandateEvaluation
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    SharedOpportunityProjection,
    SharedOpportunitySet,
)


class CounterfactualLedgerRepository(Protocol):
    def save_decision(self, *, decision: CounterfactualDecision) -> bool: ...


class SharedOpportunityEvidenceSource(Protocol):
    def opportunity_sets(self) -> tuple[SharedOpportunitySet, ...]: ...

    def eligibility_decisions(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]: ...

    def projection(self, projection_id: str) -> SharedOpportunityProjection | None: ...


class MandateEvidenceSource(Protocol):
    def all_evaluations(self) -> tuple[MandateEvaluation, ...]: ...

    def opportunity(self, projection_id: str) -> SharedOpportunityProjection | None: ...


@dataclass(frozen=True, slots=True)
class LedgerSyncResult:
    origin: str
    considered_count: int
    newly_written_count: int
    idempotent_replay_count: int
    decision_ids: tuple[str, ...]
    evaluated_at: datetime


class SyncCounterfactualLedgerFromSharedOpportunity:
    """Normalize every retained ``EligibilityDecision`` across every
    retained ``SharedOpportunitySet`` -- both eligible and rejected/expired/
    blocked -- into the ledger."""

    def __init__(
        self, repository: CounterfactualLedgerRepository, source: SharedOpportunityEvidenceSource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(self, *, evaluated_at: datetime) -> LedgerSyncResult:
        decision_ids: list[str] = []
        replay_count = 0
        for opportunity_set in self._source.opportunity_sets():
            for eligibility in self._source.eligibility_decisions(
                opportunity_set.opportunity_set_id
            ):
                projection = self._source.projection(eligibility.projection_id)
                if projection is None:
                    raise CounterfactualLedgerError(
                        "COUNTERFACTUAL_LEDGER_ELIGIBILITY_PROJECTION_MISSING"
                    )
                built = build_decision_from_eligibility(
                    decision=eligibility,
                    projection=projection,
                    opportunity_set_id=opportunity_set.opportunity_set_id,
                )
                replay = self._repository.save_decision(decision=built)
                decision_ids.append(built.decision_id)
                replay_count += 1 if replay else 0
        return LedgerSyncResult(
            origin="SHARED_OPPORTUNITY_ELIGIBILITY",
            considered_count=len(decision_ids),
            newly_written_count=len(decision_ids) - replay_count,
            idempotent_replay_count=replay_count,
            decision_ids=tuple(decision_ids),
            evaluated_at=evaluated_at,
        )


class SyncCounterfactualLedgerFromMandateEvaluations:
    """Normalize every retained ``MandateEvaluation`` -- across every
    mandate lineage/version that has ever produced one -- into the ledger."""

    def __init__(
        self, repository: CounterfactualLedgerRepository, source: MandateEvidenceSource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(self, *, evaluated_at: datetime) -> LedgerSyncResult:
        decision_ids: list[str] = []
        replay_count = 0
        for evaluation in self._source.all_evaluations():
            opportunity = self._source.opportunity(evaluation.opportunity_id)
            if opportunity is None:
                raise CounterfactualLedgerError("COUNTERFACTUAL_LEDGER_MANDATE_OPPORTUNITY_MISSING")
            built = build_decision_from_mandate_evaluation(
                evaluation=evaluation, opportunity=opportunity
            )
            replay = self._repository.save_decision(decision=built)
            decision_ids.append(built.decision_id)
            replay_count += 1 if replay else 0
        return LedgerSyncResult(
            origin="INVESTMENT_MANDATE_EVALUATION",
            considered_count=len(decision_ids),
            newly_written_count=len(decision_ids) - replay_count,
            idempotent_replay_count=replay_count,
            decision_ids=tuple(decision_ids),
            evaluated_at=evaluated_at,
        )


@dataclass(frozen=True, slots=True)
class CounterfactualLedgerSyncSummary:
    """The combined result of synchronizing both integrated origins under
    one shared ``evaluated_at`` -- what every caller that wants "the ledger
    is now current" (as opposed to "one specific origin is now current")
    actually wants back, instead of re-implementing the two-call
    composition itself."""

    evaluated_at: datetime
    eligibility_result: LedgerSyncResult
    mandate_result: LedgerSyncResult
    total_considered_count: int
    total_newly_written_count: int
    total_idempotent_replay_count: int


class SyncCounterfactualLedger:
    """Composes ``SyncCounterfactualLedgerFromSharedOpportunity`` and
    ``SyncCounterfactualLedgerFromMandateEvaluations`` under one call, so
    every caller that wants both origins synchronized together (the CLI's
    ``counterfactual-ledger-sync`` command today; the recurring operating
    cycle from Counterfactual Outcome Feedback v1 onward) shares one
    composition instead of each re-implementing "run both, with the same
    evaluated_at" ad hoc.  Neither existing service is modified: this class
    only wires them together and adds no new persistence, provider, or
    write capability of its own -- the two services it composes still do
    all of the actual reading and writing, exactly as they already did
    when invoked individually."""

    def __init__(
        self,
        repository: CounterfactualLedgerRepository,
        shared_opportunity_source: SharedOpportunityEvidenceSource,
        mandate_source: MandateEvidenceSource,
    ) -> None:
        self._from_shared_opportunity = SyncCounterfactualLedgerFromSharedOpportunity(
            repository, shared_opportunity_source
        )
        self._from_mandate = SyncCounterfactualLedgerFromMandateEvaluations(
            repository, mandate_source
        )

    def execute(self, *, evaluated_at: datetime) -> CounterfactualLedgerSyncSummary:
        eligibility_result = self._from_shared_opportunity.execute(evaluated_at=evaluated_at)
        mandate_result = self._from_mandate.execute(evaluated_at=evaluated_at)
        return CounterfactualLedgerSyncSummary(
            evaluated_at=evaluated_at,
            eligibility_result=eligibility_result,
            mandate_result=mandate_result,
            total_considered_count=(
                eligibility_result.considered_count + mandate_result.considered_count
            ),
            total_newly_written_count=(
                eligibility_result.newly_written_count + mandate_result.newly_written_count
            ),
            total_idempotent_replay_count=(
                eligibility_result.idempotent_replay_count + mandate_result.idempotent_replay_count
            ),
        )
