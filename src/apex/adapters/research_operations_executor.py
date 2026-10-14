"""Executor seam limited to already-retained Apex experiment-ledger evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.adapters.research_integrity_postgres import PostgresResearchIntegrityRepository
from apex.application.crypto_stateful_research import CryptoStatefulExperimentExecutor
from apex.application.first_generated_research_method import (
    CurrentOpportunityOperationalEvidenceSource,
    MaterializeFirstGeneratedResearchMethodRun,
)
from apex.application.research_operations import (
    COUNTERFACTUAL_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX,
    RETAINED_AGGREGATE_ONLY_FAMILY_SUFFIXES,
    SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX,
    ResearchOperationsRepository,
)
from apex.domain.counterfactual_outcome import (
    CounterfactualOutcomeAggregate,
    counterfactual_outcome_sourcing_exclusion,
)
from apex.domain.crypto_stateful_research import (
    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
)
from apex.domain.research_operations import (
    ResearchExperimentOutcome,
    ResearchOperationsError,
    ResearchProposal,
)
from apex.domain.securities_structure_outcome import SecuritiesStructureOutcomeAggregate
from apex.domain.values import content_digest

_COUNTERFACTUAL_OUTCOME_FACT_PREFIX = "counterfactual-outcome-"
_SECURITIES_STRUCTURE_OUTCOME_FACT_PREFIX = "securities-structure-outcome-"


class ExistingResearchLedgerExecutor:
    """Compare exact completed retained trials; never compile or run arbitrary code."""

    def __init__(self, repository: PostgresResearchIntegrityRepository) -> None:
        self._repository = repository

    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        configuration = dict(proposal.experiment_configuration)
        experiment_id = configuration.get("existing_experiment_id")
        if not experiment_id:
            raise ResearchOperationsError("RESEARCH_EXISTING_EXPERIMENT_ID_REQUIRED")
        family = self._repository.family(experiment_id)
        if family is None:
            raise ResearchOperationsError("RESEARCH_EXISTING_EXPERIMENT_NOT_FOUND")
        trials = tuple(
            value
            for value in self._repository.trials(experiment_id)
            if value.state.value == "COMPLETED" and value.result_hash is not None
        )
        metrics = tuple(
            metric
            for value in trials
            if (metric := self._repository.metric(value.trial_id)) is not None
        )
        evaluations = self._repository.evaluations(experiment_id)
        completed = bool(trials and len(metrics) == len(trials))
        expected_net = (
            sum((value.net_return for value in metrics), Decimal()) / Decimal(len(metrics))
            if metrics
            else None
        )
        liquidity_ids = tuple(
            value for value in proposal.required_data_ids if "LIQUIDITY" in value.upper()
        )
        comparison_improved = bool(
            evaluations
            and evaluations[-1].recommendation.value
            in {
                "ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW",
                "CONTINUE_RESEARCH",
            }
        )
        result_hash = content_digest(
            (
                family.canonical_result_hash,
                tuple((value.trial_id, value.result_hash) for value in trials),
                tuple(value.canonical_result_hash for value in metrics),
                tuple(value.canonical_result_hash for value in evaluations),
            )
        )
        return ResearchExperimentOutcome.create(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            existing_experiment_id=experiment_id,
            existing_trial_ids=tuple(value.trial_id for value in trials),
            result_hash=result_hash,
            completed=completed,
            reproducible=completed and all(value.canonical_result_hash for value in metrics),
            expected_net_economics=expected_net,
            uncertainty_sufficient=bool(
                metrics and min(value.observation_count for value in metrics) >= 120
            ),
            liquidity_sufficient=bool(liquidity_ids),
            downside_within_limit=bool(
                metrics and max(value.maximum_drawdown for value in metrics) <= Decimal("0.50")
            ),
            robust_scope_count=(
                min(value.independent_group_count for value in metrics) if metrics else 0
            ),
            comparison_improved=comparison_improved,
            warnings=(
                "EXISTING_RETAINED_EXPERIMENT_EVIDENCE_ONLY",
                "NO_NEW_STRATEGY_CODE_EXECUTED",
                *(() if liquidity_ids else ("LIQUIDITY_EVIDENCE_NOT_BOUND",)),
            ),
            completed_at=executed_at,
        )


class FirstGeneratedOperationalMethodExecutor:
    """Execute the approved retained-evidence projection with zero provider requests."""

    def __init__(
        self,
        repository: ResearchOperationsRepository,
        evidence_source: CurrentOpportunityOperationalEvidenceSource,
    ) -> None:
        self._repository = repository
        self._evidence_source = evidence_source

    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        if proposal.proposed_family != "PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION":
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_PROPOSAL_UNSUPPORTED")
        method_run, _ = MaterializeFirstGeneratedResearchMethodRun(
            self._repository, self._evidence_source
        ).execute(method_run_id=f"{run_id}-operational-evidence", completed_at=executed_at)
        return ResearchExperimentOutcome.create(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            existing_experiment_id=method_run.method_id,
            existing_trial_ids=(method_run.method_run_id,),
            result_hash=method_run.canonical_hash,
            completed=True,
            reproducible=True,
            expected_net_economics=None,
            uncertainty_sufficient=False,
            liquidity_sufficient=bool(method_run.paper_ready_proposal_ids),
            downside_within_limit=bool(method_run.allocated_line_count),
            robust_scope_count=len(method_run.source_projection_ids),
            comparison_improved=True,
            warnings=(
                "RETAINED_CURRENT_OPPORTUNITY_EVIDENCE_ONLY",
                "NO_PROVIDER_REQUESTS",
                "NO_EXPECTED_RETURN_INFERRED",
                "ZERO_ALLOCATION_IS_VALID",
                *method_run.blockers,
            ),
            completed_at=executed_at,
        )


class _CounterfactualOutcomeAggregateSource(Protocol):
    def earliest_qualified_per_cohort(self) -> tuple[CounterfactualOutcomeAggregate, ...]: ...


class _SecuritiesStructureOutcomeAggregateSource(Protocol):
    def earliest_qualified_per_cohort(self) -> tuple[SecuritiesStructureOutcomeAggregate, ...]: ...


def _outcome_from_retained_aggregate(
    aggregate: CounterfactualOutcomeAggregate | SecuritiesStructureOutcomeAggregate,
    *,
    proposal: ResearchProposal,
    run_id: str,
    executed_at: datetime,
) -> ResearchExperimentOutcome:
    """Restate one already-QUALIFIED retained aggregate as a bounded
    experiment outcome -- never recomputes, never mutates, never calls a
    provider. ``liquidity_sufficient``/``comparison_improved`` are honestly
    ``False``: a retrospective rollup over rejected or already-pursued
    decisions carries no live-position liquidity evidence and no benchmark
    comparison, so ``recommend_research_outcome`` can never overclaim
    ``PAPER_CANDIDATE`` from this evidence class alone -- the strongest
    reachable conclusions are ``REJECT`` (the aggregate confirms the current
    policy) or ``REPLICATE``/``REVISE`` (the aggregate suggests the policy
    may be miscalibrated and a human should look closer), matching this
    proposal family's own declared scope exactly."""

    # Counterfactual Outcome Evidence Specificity v3: a V3 aggregate's
    # qualified statistic is the gate-complement spread over independent
    # windows, never an absolute return (its ``mean_subsequent_return`` is
    # deliberately ``None``).  Feeding the spread as the experiment's
    # economics keeps the unmodified ``recommend_research_outcome`` policy
    # meaningful for this family -- a positive spread (the rejected set
    # outperformed what the gate let through) reads as REVISE, a
    # non-positive one as REJECT -- and the warning names the semantics so
    # no reader mistakes the number for a trade return.
    specificity = (
        aggregate.specificity if isinstance(aggregate, CounterfactualOutcomeAggregate) else None
    )
    expected_net_economics = (
        specificity.mean_relative_window_return
        if specificity is not None
        else aggregate.mean_subsequent_return
    )
    return ResearchExperimentOutcome.create(
        run_id=run_id,
        proposal_id=proposal.proposal_id,
        existing_experiment_id=aggregate.cohort_id,
        existing_trial_ids=aggregate.contributing_decision_ids,
        result_hash=aggregate.canonical_hash,
        completed=True,
        reproducible=True,
        expected_net_economics=expected_net_economics,
        uncertainty_sufficient=True,
        liquidity_sufficient=False,
        downside_within_limit=True,
        robust_scope_count=len(aggregate.contributing_decision_ids),
        comparison_improved=False,
        warnings=(
            "RETAINED_AGGREGATE_EVIDENCE_ONLY",
            "NO_NEW_STRATEGY_CODE_EXECUTED",
            "NO_PROVIDER_REQUESTS",
            "LIQUIDITY_EVIDENCE_NOT_APPLICABLE_TO_RETROSPECTIVE_POLICY_REVIEW",
            "BENCHMARK_COMPARISON_NOT_APPLICABLE_TO_RETROSPECTIVE_POLICY_REVIEW",
            *(("PEER_RELATIVE_ECONOMICS_NOT_ABSOLUTE_RETURN",) if specificity is not None else ()),
        ),
        completed_at=executed_at,
    )


class RetainedOutcomeAggregateExperimentExecutor:
    """Execute a ``COUNTERFACTUAL_OUTCOME_POLICY_REVIEW``/``SECURITIES_
    STRUCTURE_OUTCOME_POLICY_REVIEW`` proposal directly from the single
    already-QUALIFIED aggregate its own agenda item is anchored to --
    exactly the ``RETAINED_..._AGGREGATE_ONLY`` scope those proposals
    already declare in ``experiment_configuration``. Reuses
    ``earliest_qualified_per_cohort`` (the identical, already-deterministic
    method the research-agenda source itself calls) so the anchored
    aggregate this executor reads is provably the same one, forever, for a
    given cohort -- required for ``RunGovernedResearchExperiment``'s own
    replay-divergence check to hold across repeated runs of the same
    ``run_id`` even as new, later aggregate rows for that cohort keep
    accumulating on other, unrelated days."""

    def __init__(
        self,
        *,
        counterfactual_outcome: _CounterfactualOutcomeAggregateSource,
        securities_structure_outcome: _SecuritiesStructureOutcomeAggregateSource,
    ) -> None:
        self._counterfactual_outcome = counterfactual_outcome
        self._securities_structure_outcome = securities_structure_outcome

    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        family = proposal.proposed_family
        if family.endswith(COUNTERFACTUAL_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX):
            prefix = _COUNTERFACTUAL_OUTCOME_FACT_PREFIX
            aggregates: (
                tuple[CounterfactualOutcomeAggregate, ...]
                | tuple[SecuritiesStructureOutcomeAggregate, ...]
            ) = self._counterfactual_outcome.earliest_qualified_per_cohort()
        elif family.endswith(SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX):
            prefix = _SECURITIES_STRUCTURE_OUTCOME_FACT_PREFIX
            aggregates = self._securities_structure_outcome.earliest_qualified_per_cohort()
        else:
            raise ResearchOperationsError("RETAINED_OUTCOME_AGGREGATE_PROPOSAL_FAMILY_UNSUPPORTED")
        cohort_id = next(
            (
                value.removeprefix(prefix)
                for value in proposal.required_data_ids
                if value.startswith(prefix)
            ),
            None,
        )
        if cohort_id is None:
            raise ResearchOperationsError("RETAINED_OUTCOME_AGGREGATE_COHORT_ID_MISSING")
        aggregate = next((value for value in aggregates if value.cohort_id == cohort_id), None)
        if aggregate is None:
            raise ResearchOperationsError("RETAINED_OUTCOME_AGGREGATE_NO_LONGER_QUALIFIED")
        # Counterfactual V2 Research Evidence Quarantine v1. Applied here as
        # well as at the agenda source, not instead of it: the 13 proposals
        # already retained against quarantined V2 CRYPTO cohorts are
        # deliberately left in place (nothing this milestone does closes or
        # deletes a retained artifact), and every one of them still names
        # its cohort in ``required_data_ids``. Without this second check,
        # any one of them becoming feasible later -- a duplicate
        # classification changing, an implementation landing -- would route
        # straight back into defective evidence through a path the agenda
        # filter never sees.
        #
        # Raised rather than silently degraded: an experiment that cannot
        # honestly run must not report an outcome, and this reason code is
        # distinct from ``..._NO_LONGER_QUALIFIED`` so the two can never be
        # confused in a diagnostic (the cohort *is* still qualified; its
        # evidence policy version is what is disqualified).
        if isinstance(aggregate, CounterfactualOutcomeAggregate) and (
            counterfactual_outcome_sourcing_exclusion(
                venue=aggregate.venue,
                outcome_policy_version=aggregate.outcome_policy_version,
                followup_policy_version=aggregate.followup_policy_version,
                baseline_kind=aggregate.baseline_kind,
            )
            is not None
        ):
            raise ResearchOperationsError(
                "RETAINED_OUTCOME_AGGREGATE_EVIDENCE_POLICY_VERSION_QUARANTINED"
            )
        return _outcome_from_retained_aggregate(
            aggregate, proposal=proposal, run_id=run_id, executed_at=executed_at
        )


class GovernedResearchExecutor:
    """Route only explicitly implemented methods; retain the existing ledger executor."""

    def __init__(
        self,
        *,
        ledger: ExistingResearchLedgerExecutor,
        operational: FirstGeneratedOperationalMethodExecutor,
        stateful_crypto: CryptoStatefulExperimentExecutor | None = None,
        outcome_aggregate: RetainedOutcomeAggregateExperimentExecutor | None = None,
    ) -> None:
        self._ledger = ledger
        self._operational = operational
        self._stateful_crypto = stateful_crypto
        self._outcome_aggregate = outcome_aggregate

    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        if proposal.proposed_family == "PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION":
            return self._operational.execute(
                proposal=proposal, run_id=run_id, executed_at=executed_at
            )
        if (
            dict(proposal.strategy_definition).get("method_id")
            == CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1
        ):
            if self._stateful_crypto is None:
                raise ResearchOperationsError("CRYPTO_STATEFUL_EXECUTOR_NOT_CONFIGURED")
            return self._stateful_crypto.execute(
                proposal=proposal, run_id=run_id, executed_at=executed_at
            )
        if proposal.proposed_family.endswith(RETAINED_AGGREGATE_ONLY_FAMILY_SUFFIXES):
            if self._outcome_aggregate is None:
                raise ResearchOperationsError("RETAINED_OUTCOME_AGGREGATE_EXECUTOR_NOT_CONFIGURED")
            return self._outcome_aggregate.execute(
                proposal=proposal, run_id=run_id, executed_at=executed_at
            )
        return self._ledger.execute(proposal=proposal, run_id=run_id, executed_at=executed_at)
