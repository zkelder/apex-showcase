"""Application layer for Strategy PAPER Trial Outcome Assessment v1.

``AssessClosedStrategyPaperTrials`` discovers every retained closed trial
that has no assessment under the current policy -- including trials that
closed before this capability existed -- gathers its immutable retained
facts, and appends one assessment per trial. Every decision is made by the
pure functions in ``domain.strategy_trial_outcome_assessment``.

Read-only toward everything it does not own. It never imports
``application.strategy_paper_trial``/``application.crypto_paper`` (the only
code that can place or change a PAPER order), makes no provider request,
and writes nothing but ``strategy_trial_outcome_assessment`` rows.

Failure isolation: a lineage or economics error on one trial is reported
as that trial's own ``failed`` code, and every other trial proceeds. A
trial whose final attribution is not yet materialized is deferred, never
assessed without it. Only an infrastructure error (e.g. the database being
unavailable) propagates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.crypto_paper import CryptoPaperState
from apex.domain.performance_attribution import PaperEvidenceClass, SharedPerformanceAttribution
from apex.domain.strategy_evaluation_history import InternalStrategyEvaluationRecord
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialClosureRecord,
    StrategyPaperTrialRecord,
)
from apex.domain.strategy_trial_outcome_assessment import (
    STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1,
    STRATEGY_TRIAL_OUTCOME_ASSESSMENT_WARNING,
    STRATEGY_TRIAL_OUTCOME_MAX_ASSESSMENTS_PER_CYCLE,
    STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE,
    ClosedTrialFacts,
    StrategyTrialOutcomeAssessment,
    StrategyTrialOutcomeAssessmentError,
    TrialEconomicsState,
    build_strategy_trial_outcome_assessment,
    build_trial_attribution_lineage,
    build_trial_version_lineage,
    derive_trial_economics,
    expected_final_attribution_sources,
    strategy_trial_outcome_assessment_id,
)
from apex.domain.strategy_version_provenance import (
    StrategyPaperTrialProvenance,
    StrategyVersionProvenanceError,
    StrategyVersionTransition,
    TrialProvenanceClass,
    classify_trial_provenance,
)
from apex.domain.values import ValueValidationError


class StrategyTrialOutcomeAssessmentResearchError(ValueError):
    """A persistence action could not honestly proceed (identity conflict).
    Fails closed."""


class StrategyTrialOutcomeAssessmentRepository(Protocol):
    def register(
        self, *, assessment: StrategyTrialOutcomeAssessment, assessed_at: datetime
    ) -> bool:
        """Insert-or-replay. ``True`` for an identical replay; raises
        ``StrategyTrialOutcomeAssessmentResearchError`` on a different hash
        for the same identity."""
        ...

    def get(self, *, assessment_id: str) -> StrategyTrialOutcomeAssessment | None: ...

    def assessed_trial_ids(self, *, policy_version: str) -> frozenset[str]: ...

    def assessments(self) -> tuple[StrategyTrialOutcomeAssessment, ...]: ...


class _Trials(Protocol):
    def get(self, *, trial_id: str) -> StrategyPaperTrialRecord | None: ...


class _Closures(Protocol):
    def all_closures(self) -> tuple[StrategyPaperTrialClosureRecord, ...]: ...


class _Provenance(Protocol):
    def trial_provenance(self, *, trial_id: str) -> StrategyPaperTrialProvenance | None: ...

    def transition(self, *, transition_id: str) -> StrategyVersionTransition | None: ...


class _Evaluations(Protocol):
    def get(self, *, evaluation_id: str) -> InternalStrategyEvaluationRecord | None: ...


class _PaperStates(Protocol):
    def state(self, account_id: str) -> CryptoPaperState | None: ...


class _Attributions(Protocol):
    def attribution_for_source(
        self, source_id: str, source_hash: str, evidence_class: PaperEvidenceClass
    ) -> SharedPerformanceAttribution | None: ...


class InMemoryStrategyTrialOutcomeAssessmentRepository:
    """Reference/test implementation with the same replay/conflict contract."""

    def __init__(self) -> None:
        self._rows: dict[str, StrategyTrialOutcomeAssessment] = {}
        self.assessed_at: dict[str, datetime] = {}

    def register(
        self, *, assessment: StrategyTrialOutcomeAssessment, assessed_at: datetime
    ) -> bool:
        existing = self._rows.get(assessment.assessment_id)
        if existing is not None:
            if existing.canonical_hash != assessment.canonical_hash:
                raise StrategyTrialOutcomeAssessmentResearchError(
                    "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_IDENTITY_CONFLICT"
                )
            return True
        self._rows[assessment.assessment_id] = assessment
        self.assessed_at[assessment.assessment_id] = assessed_at
        return False

    def get(self, *, assessment_id: str) -> StrategyTrialOutcomeAssessment | None:
        return self._rows.get(assessment_id)

    def assessed_trial_ids(self, *, policy_version: str) -> frozenset[str]:
        return frozenset(
            item.trial_id for item in self._rows.values() if item.policy_version == policy_version
        )

    def assessments(self) -> tuple[StrategyTrialOutcomeAssessment, ...]:
        return tuple(sorted(self._rows.values(), key=lambda item: (item.closed_at, item.trial_id)))


@dataclass(frozen=True, slots=True)
class AssessClosedStrategyPaperTrialsResult:
    written: tuple[str, ...]
    replayed: tuple[str, ...]
    deferred: tuple[str, ...]
    failed: tuple[str, ...]
    backlog_remaining: int
    policy_version: str = STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1
    authority: str = "NONE"


_FoundAttribution = tuple[
    SharedPerformanceAttribution,
    SharedPerformanceAttribution | None,
    tuple[str, str],
    tuple[str, str] | None,
]


@dataclass(frozen=True, slots=True)
class _Gathered:
    facts: dict[str, ClosedTrialFacts]
    failed: dict[str, str]
    states: dict[str, CryptoPaperState | None]


class AssessClosedStrategyPaperTrials:
    """Append one outcome assessment per closed, not-yet-assessed trial."""

    def __init__(
        self,
        *,
        assessments: StrategyTrialOutcomeAssessmentRepository,
        trials: _Trials,
        closures: _Closures,
        provenance: _Provenance,
        evaluations: _Evaluations,
        paper_states: _PaperStates,
        attributions: _Attributions,
    ) -> None:
        self._assessments = assessments
        self._trials = trials
        self._closures = closures
        self._provenance = provenance
        self._evaluations = evaluations
        self._paper_states = paper_states
        self._attributions = attributions

    def _gather(self, closures: Sequence[StrategyPaperTrialClosureRecord]) -> _Gathered:
        facts: dict[str, ClosedTrialFacts] = {}
        failed: dict[str, str] = {}
        states: dict[str, CryptoPaperState | None] = {}
        for closure in closures:
            trial = self._trials.get(trial_id=closure.trial_id)
            if trial is None:
                failed[closure.trial_id] = "STRATEGY_TRIAL_OUTCOME_TRIAL_NOT_FOUND"
                continue
            if trial.account_id not in states:
                states[trial.account_id] = self._paper_states.state(trial.account_id)
            state = states[trial.account_id]
            if state is None:
                failed[trial.trial_id] = "STRATEGY_TRIAL_OUTCOME_PAPER_ACCOUNT_NOT_FOUND"
                continue
            try:
                provenance = self._provenance.trial_provenance(trial_id=trial.trial_id)
                provenance_class = classify_trial_provenance(trial=trial, provenance=provenance)
                lineage = None
                if provenance_class is TrialProvenanceClass.BOUND and provenance is not None:
                    transition = self._provenance.transition(transition_id=provenance.transition_id)
                    if transition is None:
                        raise StrategyTrialOutcomeAssessmentError(
                            "STRATEGY_TRIAL_OUTCOME_TRANSITION_NOT_FOUND"
                        )
                    evaluation = self._evaluations.get(evaluation_id=transition.evaluation_id)
                    if evaluation is None:
                        raise StrategyTrialOutcomeAssessmentError(
                            "STRATEGY_TRIAL_OUTCOME_EVALUATION_NOT_FOUND"
                        )
                    lineage = build_trial_version_lineage(
                        trial=trial,
                        provenance=provenance,
                        transition=transition,
                        evaluation=evaluation,
                    )
                facts[trial.trial_id] = ClosedTrialFacts(
                    trial=trial,
                    closure=closure,
                    provenance_class=provenance_class,
                    lineage=lineage,
                    economics=derive_trial_economics(
                        trial=trial, closure=closure, fills=state.fills
                    ),
                )
            except (StrategyTrialOutcomeAssessmentError, StrategyVersionProvenanceError) as error:
                failed[trial.trial_id] = str(error)
        return _Gathered(facts=facts, failed=failed, states=states)

    def _attribution(
        self, subject: ClosedTrialFacts, state: CryptoPaperState
    ) -> _FoundAttribution | None:
        orders = {item.order_id: item for item in state.orders}
        opening_order = orders.get(subject.trial.opening_order_id)
        if opening_order is None:
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_OPENING_ORDER_NOT_FOUND"
            )
        closing_order = orders.get(subject.closure.closing_order_id)
        opening_source, closing_source = expected_final_attribution_sources(
            trial=subject.trial,
            closure=subject.closure,
            opening_order_hash=opening_order.canonical_hash,
            closing_order_hash=None if closing_order is None else closing_order.canonical_hash,
        )
        opening = self._attributions.attribution_for_source(
            *opening_source, PaperEvidenceClass.GENUINE_PAPER
        )
        closing = (
            None
            if closing_source is None
            else self._attributions.attribution_for_source(
                *closing_source, PaperEvidenceClass.GENUINE_PAPER
            )
        )
        if opening is None or (closing_source is not None and closing is None):
            return None
        return opening, closing, opening_source, closing_source

    def execute(
        self,
        *,
        assessed_at: datetime,
        limit: int = STRATEGY_TRIAL_OUTCOME_MAX_ASSESSMENTS_PER_CYCLE,
    ) -> AssessClosedStrategyPaperTrialsResult:
        if limit < 1:
            raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_LIMIT_INVALID")
        closures = sorted(
            self._closures.all_closures(), key=lambda item: (item.closed_at, item.trial_id)
        )
        assessed = self._assessments.assessed_trial_ids(
            policy_version=STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1
        )
        pending = [item for item in closures if item.trial_id not in assessed]
        if not pending:
            return AssessClosedStrategyPaperTrialsResult((), (), (), (), 0)
        gathered = self._gather(closures)
        candidates = tuple(gathered.facts.values())
        written: list[str] = []
        replayed: list[str] = []
        deferred: list[str] = []
        failed: list[str] = []
        backlog_remaining = 0
        for closure in pending:
            trial_id = closure.trial_id
            if trial_id in gathered.failed:
                failed.append(f"{trial_id}:{gathered.failed[trial_id]}")
                continue
            if len(written) >= limit:
                backlog_remaining += 1
                continue
            subject = gathered.facts[trial_id]
            state = gathered.states[subject.trial.account_id]
            if state is None:  # pragma: no cover - excluded by _gather
                continue
            try:
                found = self._attribution(subject, state)
                if found is None:
                    deferred.append(f"{trial_id}:AWAITING_CLOSED_ATTRIBUTION")
                    continue
                opening, closing, opening_source, closing_source = found
                assessment = build_strategy_trial_outcome_assessment(
                    subject=subject,
                    candidates=candidates,
                    attribution=build_trial_attribution_lineage(
                        trial=subject.trial,
                        opening=opening,
                        closing=closing,
                        expected_opening_source=opening_source,
                        expected_closing_source=closing_source,
                    ),
                )
            except StrategyTrialOutcomeAssessmentError as error:
                failed.append(f"{trial_id}:{error}")
                continue
            except ValueValidationError as error:
                # A value that cannot be represented canonically is this
                # trial's own failure, never the whole task's.
                failed.append(f"{trial_id}:ValueValidationError:{error}")
                continue
            try:
                replay = self._assessments.register(assessment=assessment, assessed_at=assessed_at)
            except StrategyTrialOutcomeAssessmentResearchError as error:
                # Identity conflict or a database-level lineage rejection:
                # nothing was written for this trial; others still proceed.
                failed.append(f"{trial_id}:{error}")
                continue
            if replay:
                replayed.append(assessment.assessment_id)
            else:
                written.append(assessment.assessment_id)
        return AssessClosedStrategyPaperTrialsResult(
            written=tuple(written),
            replayed=tuple(replayed),
            deferred=tuple(deferred),
            failed=tuple(failed),
            backlog_remaining=backlog_remaining,
        )


def strategy_trial_outcome_report(
    *,
    assessments: Sequence[StrategyTrialOutcomeAssessment],
    closures: Sequence[StrategyPaperTrialClosureRecord],
    active_trials: Sequence[StrategyPaperTrialRecord],
    strategy_id: str | None = None,
    trial_id: str | None = None,
) -> dict[str, object]:
    """Read-only operator view: every assessment, every closed trial still
    awaiting one, active trials (never assessed while open), per-status
    counts, and each population's current size against the minimum."""

    def selected(item_strategy_id: str, item_trial_id: str) -> bool:
        return (strategy_id is None or item_strategy_id == strategy_id) and (
            trial_id is None or item_trial_id == trial_id
        )

    chosen = sorted(
        (item for item in assessments if selected(item.strategy_id, item.trial_id)),
        key=lambda item: (item.closed_at, item.trial_id),
    )
    assessed_ids = {
        item.trial_id
        for item in assessments
        if item.policy_version == STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1
    }
    unassessed = sorted(
        (
            item
            for item in closures
            if item.trial_id not in assessed_ids and selected(item.strategy_id, item.trial_id)
        ),
        key=lambda item: (item.closed_at, item.trial_id),
    )
    status_counts: dict[str, int] = {}
    for item in chosen:
        status_counts[item.status.value] = status_counts.get(item.status.value, 0) + 1
    populations: dict[str, dict[str, object]] = {}
    for item in chosen:
        if item.population_id is None:
            continue
        populations[item.population_id] = {
            "population_id": item.population_id,
            "strategy_id": item.strategy_id,
            "definition_hash": item.definition_hash,
            "instrument_id": item.instrument_id,
            "latest_population_size": item.population_size,
            "minimum_sample_size": item.minimum_sample_size,
            "latest_assessment_id": item.assessment_id,
        }
    return {
        "policy_version": STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1,
        "minimum_sample_size": STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE,
        "assessment_count": len(chosen),
        "status_counts": dict(sorted(status_counts.items())),
        "provenance_bound_sample_count": sum(
            1
            for item in chosen
            if item.provenance_class is TrialProvenanceClass.BOUND
            and item.realized_return is not None
            and item.economics_state is TrialEconomicsState.RECONCILED
        ),
        "assessments": tuple(chosen),
        "closed_trials_awaiting_assessment": tuple(
            {"trial_id": item.trial_id, "closure_id": item.closure_id, "closed_at": item.closed_at}
            for item in unassessed
        ),
        "active_trials_not_assessable_while_open": tuple(
            {"trial_id": item.trial_id, "generation": item.generation, "opened_at": item.opened_at}
            for item in active_trials
            if selected(item.strategy_id, item.trial_id)
        ),
        "populations": tuple(populations[key] for key in sorted(populations)),
        "authority": "NONE",
        "live_authority": "NONE",
        "warning": STRATEGY_TRIAL_OUTCOME_ASSESSMENT_WARNING,
    }


def strategy_trial_outcome_summary(item: StrategyTrialOutcomeAssessment) -> dict[str, object]:
    """One compact operator row: lineage, expected-evidence basis, realized
    evidence, status, and sample position. Values are the retained exact
    ``Decimal``s, never console-formatted numbers."""

    return {
        "assessment_id": item.assessment_id,
        "trial_id": item.trial_id,
        "generation": item.generation,
        "strategy_id": item.strategy_id,
        "strategy_version": item.strategy_version,
        "provenance_class": item.provenance_class.value,
        "definition_hash": item.definition_hash,
        "transition_id": item.transition_id,
        "evaluation_id": item.evaluation_id,
        "closure_id": item.closure_id,
        "terminal_reason": item.terminal_reason.value,
        "opened_at": item.opened_at,
        "closed_at": item.closed_at,
        "expected": {
            "basis": item.expected_return_basis.value,
            "evaluation_net_return": item.evaluation_net_return,
            "evaluation_window_start": item.evaluation_window_start,
            "evaluation_window_end": item.evaluation_window_end,
            "evaluation_sample_size": item.evaluation_sample_size,
            "delta_computed": False,
        },
        "realized": {
            "economics_state": item.economics_state.value,
            "opening_cost": item.opening_cost,
            "closing_net_proceeds": item.closing_net_proceeds,
            "ledger_realized_pnl": item.ledger_realized_pnl,
            "cash_flow_pnl": item.cash_flow_pnl,
            "rounding_residual": item.rounding_residual,
            "realized_return": item.realized_return,
            "basis": item.realized_return_basis,
        },
        "status": item.status.value,
        "population_size": item.population_size,
        "minimum_sample_size": item.minimum_sample_size,
        "policy_version": item.policy_version,
        "authority": item.authority,
    }


__all__ = [
    "AssessClosedStrategyPaperTrials",
    "AssessClosedStrategyPaperTrialsResult",
    "InMemoryStrategyTrialOutcomeAssessmentRepository",
    "StrategyTrialOutcomeAssessmentRepository",
    "StrategyTrialOutcomeAssessmentResearchError",
    "strategy_trial_outcome_assessment_id",
    "strategy_trial_outcome_report",
    "strategy_trial_outcome_summary",
]
