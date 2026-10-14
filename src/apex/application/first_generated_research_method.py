"""Application services for one approved generated operational research method."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from apex.application.research_operations import (
    EmptyResearchKnowledgeSource,
    ResearchKnowledgeSource,
    ResearchOperationsRepository,
    ReviewResearchProposal,
    duplicate_decision,
)
from apex.domain.first_generated_research_method import (
    CurrentOpportunityOperationalEvidence,
    DeferredGeneratedProposal,
    FirstGeneratedResearchMethodImplementation,
    FirstGeneratedResearchMethodRun,
    FirstGeneratedResearchMethodSelection,
    select_first_generated_proposal,
)
from apex.domain.research_operations import (
    ResearchFeasibilityState,
    ResearchOperationsError,
    ResearchProposalFeasibility,
    ResearchProposalState,
)


class CurrentOpportunityOperationalEvidenceSource(Protocol):
    def evidence(self) -> CurrentOpportunityOperationalEvidence: ...


@dataclass(frozen=True, slots=True)
class FirstGeneratedMethodSelectionResult:
    selection: FirstGeneratedResearchMethodSelection
    implementation: FirstGeneratedResearchMethodImplementation
    feasibility: ResearchProposalFeasibility
    idempotent_replay: bool


class SelectApproveAndImplementFirstGeneratedMethod:
    """Select rank 2 when rank 1 is underspecified, then retain exact governance."""

    def __init__(
        self,
        repository: ResearchOperationsRepository,
        knowledge_source: ResearchKnowledgeSource | None = None,
    ) -> None:
        self._repository = repository
        self._knowledge = knowledge_source or EmptyResearchKnowledgeSource()

    def execute(
        self,
        *,
        generator_run_id: str,
        selection_id: str,
        implementation_id: str,
        actor: str,
        selected_at: object,
    ) -> FirstGeneratedMethodSelectionResult:
        from datetime import datetime

        if not isinstance(selected_at, datetime):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_TIME_INVALID")
        retained_selection = self._repository.first_method_selection()
        retained_implementation = self._repository.first_method_implementation()
        if retained_selection is not None or retained_implementation is not None:
            if (
                retained_selection is None
                or retained_implementation is None
                or retained_selection.selection_id != selection_id
                or retained_implementation.implementation_id != implementation_id
            ):
                raise ResearchOperationsError("FIRST_GENERATED_METHOD_SINGLE_SELECTION_CONFLICT")
            feasibility = self._repository.proposal_feasibility(
                retained_selection.selected_proposal_id
            )
            if feasibility is None:
                raise ResearchOperationsError("FIRST_GENERATED_METHOD_FEASIBILITY_MISSING")
            if feasibility.implementation_id != retained_implementation.implementation_id:
                proposal = self._repository.proposal(retained_selection.selected_proposal_id)
                if proposal is None:
                    raise ResearchOperationsError("FIRST_GENERATED_METHOD_PROPOSAL_MISSING")
                feasibility = ResearchProposalFeasibility.create(
                    feasibility_id=f"{feasibility.feasibility_id}-implemented-v1",
                    generator_run_id=retained_selection.generator_run_id,
                    proposal=proposal,
                    duplicate_decision=duplicate_decision(
                        self._repository, proposal, self._knowledge
                    ),
                    schema_valid=True,
                    data_available=True,
                    provider_capability_available=True,
                    experiment_engine_compatible=True,
                    budget_available=True,
                    evidence_class_compatible=True,
                    authority_restrictions_passed=True,
                    evaluated_at=retained_selection.selected_at + timedelta(seconds=3),
                    implementation_id=retained_implementation.implementation_id,
                    supersedes_feasibility_id=feasibility.feasibility_id,
                )
                self._repository.save_proposal_feasibility(feasibility)
            if feasibility.status is not ResearchFeasibilityState.READY_FOR_REVIEW:
                raise ResearchOperationsError("FIRST_GENERATED_METHOD_IMPLEMENTATION_NOT_READY")
            return FirstGeneratedMethodSelectionResult(
                retained_selection, retained_implementation, feasibility, True
            )
        generator = self._repository.generator_run(generator_run_id)
        if generator is None or len(generator.generated_proposal_ids) != 3:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_GENERATOR_RUN_INVALID")
        proposals = tuple(
            value
            for proposal_id in generator.generated_proposal_ids
            if (value := self._repository.proposal(proposal_id)) is not None
        )
        priorities = tuple(
            priority_value
            for proposal in proposals
            if (priority_value := self._repository.proposal_priority(proposal.proposal_id))
            is not None
        )
        selected, priority, deferral_specs = select_first_generated_proposal(proposals, priorities)
        proposal_by_id = {value.proposal_id: value for value in proposals}
        priority_by_id = {value.proposal_id: value for value in priorities}
        deferred = tuple(
            DeferredGeneratedProposal.create(
                proposal=proposal_by_id[proposal_id],
                priority=priority_by_id[proposal_id],
                reason=reason,
                unblocking_capability=unblocker,
            )
            for proposal_id, reason, unblocker in deferral_specs
        )
        selection = FirstGeneratedResearchMethodSelection.create(
            selection_id=selection_id,
            generator_run_id=generator.generator_run_id,
            generator_run_hash=generator.canonical_hash,
            selected_proposal=selected,
            selected_priority=priority,
            rationale=(
                "Rank 1 is materially interesting but omits exact lookbacks and transition "
                "thresholds; rank 2 is the highest-ranked implementation-ready operational "
                "proposal and reuses unchanged retained-evidence policies."
            ),
            implementation_scope="EXISTING_METHOD_OPERATIONAL_SEAM_ONLY",
            approved_data_ids=selected.required_data_ids,
            deferred=deferred,
            actor=actor,
            selected_at=selected_at,
        )
        self._repository.save_first_method_selection(selection)
        reviewer = ReviewResearchProposal(self._repository, self._knowledge)
        for proposal in proposals:
            decision = duplicate_decision(self._repository, proposal, self._knowledge)
            reviewer.execute(
                proposal_id=proposal.proposal_id,
                target_state=ResearchProposalState.READY_FOR_REVIEW,
                actor=actor,
                reason="FIRST_GENERATED_METHOD_OPERATOR_REVIEW_OPENED",
                confirmation=(f"READY_FOR_REVIEW:{proposal.proposal_id}:{decision.canonical_hash}"),
                reviewed_at=selected_at,
            )
        selected_decision = duplicate_decision(self._repository, selected, self._knowledge)
        selected_review, _, _ = reviewer.execute(
            proposal_id=selected.proposal_id,
            target_state=ResearchProposalState.APPROVED_FOR_EXPERIMENT,
            actor=actor,
            reason="APPROVE_ONE_BOUNDED_OPERATIONAL_METHOD_IMPLEMENTATION",
            confirmation=(
                f"APPROVED_FOR_EXPERIMENT:{selected.proposal_id}:{selected_decision.canonical_hash}"
            ),
            reviewed_at=selected_at + timedelta(seconds=1),
            idempotency_key=f"{selection_id}-approval",
            approved_experiment_id="PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION_V1",
            approved_data_ids=selection.approved_data_ids,
            implementation_scope=selection.implementation_scope,
            provider_request_limit=0,
            deferred_proposal_ids=tuple(item.proposal_id for item in selection.deferred),
        )
        for deferred_value in selection.deferred:
            proposal = proposal_by_id[deferred_value.proposal_id]
            decision = duplicate_decision(self._repository, proposal, self._knowledge)
            reviewer.execute(
                proposal_id=proposal.proposal_id,
                target_state=ResearchProposalState.DEFERRED,
                actor=actor,
                reason=(
                    f"{deferred_value.reason};UNBLOCKER={deferred_value.unblocking_capability};"
                    "PRIORITY_UNCHANGED"
                ),
                confirmation=f"DEFERRED:{proposal.proposal_id}:{decision.canonical_hash}",
                reviewed_at=selected_at + timedelta(seconds=1),
            )
        approval = self._repository.experiment_approval(selected.proposal_id)
        if approval is None or approval.review_id != selected_review.review_id:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_APPROVAL_NOT_RETAINED")
        implementation = FirstGeneratedResearchMethodImplementation.create(
            implementation_id=implementation_id,
            selection=selection,
            proposal=selected,
            approval_id=approval.approval_id,
            approval_hash=approval.canonical_hash,
            reused_policy_ids=(
                "SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1",
                "SHARED_OPPORTUNITY_PRIORITY_POLICY_V1",
                "TOTAL_MARKET_CONTEXT_POLICY_V1",
                "CROSS_VENUE_ALLOCATION_POLICY_V1",
            ),
            implemented_at=selected_at + timedelta(seconds=2),
        )
        self._repository.save_first_method_implementation(implementation)
        prior_feasibility = self._repository.proposal_feasibility(selected.proposal_id)
        if prior_feasibility is None:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_PRIOR_FEASIBILITY_MISSING")
        duplicate = duplicate_decision(self._repository, selected, self._knowledge)
        feasibility = ResearchProposalFeasibility.create(
            feasibility_id=f"{prior_feasibility.feasibility_id}-implemented-v1",
            generator_run_id=generator.generator_run_id,
            proposal=selected,
            duplicate_decision=duplicate,
            schema_valid=True,
            data_available=True,
            provider_capability_available=True,
            experiment_engine_compatible=True,
            budget_available=True,
            evidence_class_compatible=True,
            authority_restrictions_passed=True,
            evaluated_at=selected_at + timedelta(seconds=3),
            implementation_id=implementation.implementation_id,
            supersedes_feasibility_id=prior_feasibility.feasibility_id,
        )
        if feasibility.status is not ResearchFeasibilityState.READY_FOR_REVIEW:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_IMPLEMENTATION_NOT_READY")
        self._repository.save_proposal_feasibility(feasibility)
        return FirstGeneratedMethodSelectionResult(selection, implementation, feasibility, False)


class MaterializeFirstGeneratedResearchMethodRun:
    def __init__(
        self,
        repository: ResearchOperationsRepository,
        evidence_source: CurrentOpportunityOperationalEvidenceSource,
    ) -> None:
        self._repository = repository
        self._evidence_source = evidence_source

    def execute(
        self, *, method_run_id: str, completed_at: object
    ) -> tuple[FirstGeneratedResearchMethodRun, bool]:
        from datetime import datetime

        if not isinstance(completed_at, datetime):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_TIME_INVALID")
        retained = self._repository.first_method_run(method_run_id)
        if retained is not None:
            return retained, True
        implementation = self._repository.first_method_implementation()
        if implementation is None:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_IMPLEMENTATION_MISSING")
        run = FirstGeneratedResearchMethodRun.create(
            method_run_id=method_run_id,
            implementation=implementation,
            evidence=self._evidence_source.evidence(),
            completed_at=completed_at,
        )
        self._repository.save_first_method_run(run)
        return run, False
