"""Pure rules for the first generated research-method implementation.

The implemented method is an authority-free operational projection over exact
retained evidence.  It adds no signal, price forecast, allocation policy, or
provider capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from apex.domain.research_operations import (
    FIRST_GENERATED_RESEARCH_METHOD_IMPLEMENTATION_V1,
    FIRST_GENERATED_RESEARCH_METHOD_SELECTION_V1,
    PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION_V1,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalClass,
    ResearchProposalPriority,
)
from apex.domain.values import content_digest


def _time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ResearchOperationsError("FIRST_GENERATED_METHOD_TIME_INVALID")
    return value.astimezone(UTC)


def _text(value: str, reason: str, maximum: int = 1000) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ResearchOperationsError(reason)
    return normalized


@dataclass(frozen=True, slots=True)
class DeferredGeneratedProposal:
    proposal_id: str
    proposal_hash: str
    deterministic_rank: int
    priority_hash: str
    reason: str
    unblocking_capability: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        proposal: ResearchProposal,
        priority: ResearchProposalPriority,
        reason: str,
        unblocking_capability: str,
    ) -> DeferredGeneratedProposal:
        if priority.proposal_id != proposal.proposal_id:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_PRIORITY_BINDING_INVALID")
        values = (
            proposal.proposal_id,
            proposal.canonical_hash,
            priority.deterministic_rank,
            priority.canonical_hash,
            _text(reason, "FIRST_GENERATED_METHOD_DEFERRAL_REASON_INVALID"),
            _text(
                unblocking_capability,
                "FIRST_GENERATED_METHOD_DEFERRAL_UNBLOCKER_INVALID",
            ),
        )
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class FirstGeneratedResearchMethodSelection:
    selection_id: str
    generator_run_id: str
    generator_run_hash: str
    selected_proposal_id: str
    selected_proposal_hash: str
    selected_priority_hash: str
    selected_rank: int
    rationale: str
    implementation_scope: str
    approved_data_ids: tuple[str, ...]
    provider_request_limit: int
    experiment_budget: int
    deferred: tuple[DeferredGeneratedProposal, ...]
    actor: str
    selected_at: datetime
    policy_id: str
    canonical_hash: str
    authority: str = "NONE"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        selection_id: str,
        generator_run_id: str,
        generator_run_hash: str,
        selected_proposal: ResearchProposal,
        selected_priority: ResearchProposalPriority,
        rationale: str,
        implementation_scope: str,
        approved_data_ids: tuple[str, ...],
        deferred: tuple[DeferredGeneratedProposal, ...],
        actor: str,
        selected_at: datetime,
    ) -> FirstGeneratedResearchMethodSelection:
        if selected_priority.proposal_id != selected_proposal.proposal_id:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_PRIORITY_BINDING_INVALID")
        if len(deferred) != 2 or len({item.proposal_id for item in deferred}) != 2:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_EXACTLY_TWO_DEFERRALS_REQUIRED")
        if selected_proposal.proposal_id in {item.proposal_id for item in deferred}:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_SELECTION_OVERLAP")
        data_ids = tuple(sorted(set(approved_data_ids)))
        if not set(selected_proposal.required_data_ids).issubset(data_ids):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_APPROVED_DATA_INVALID")
        values = (
            _text(selection_id, "FIRST_GENERATED_METHOD_SELECTION_ID_INVALID", 300),
            _text(generator_run_id, "FIRST_GENERATED_METHOD_GENERATOR_ID_INVALID", 300),
            _text(generator_run_hash, "FIRST_GENERATED_METHOD_GENERATOR_HASH_INVALID", 64),
            selected_proposal.proposal_id,
            selected_proposal.canonical_hash,
            selected_priority.canonical_hash,
            selected_priority.deterministic_rank,
            _text(rationale, "FIRST_GENERATED_METHOD_RATIONALE_INVALID", 2000),
            _text(implementation_scope, "FIRST_GENERATED_METHOD_SCOPE_INVALID", 1000),
            data_ids,
            0,
            1,
            tuple(sorted(deferred, key=lambda item: item.deterministic_rank)),
            _text(actor, "FIRST_GENERATED_METHOD_ACTOR_INVALID", 300),
            _time(selected_at),
            FIRST_GENERATED_RESEARCH_METHOD_SELECTION_V1,
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class FirstGeneratedResearchMethodImplementation:
    implementation_id: str
    selection_id: str
    selection_hash: str
    proposal_id: str
    proposal_hash: str
    approval_id: str
    approval_hash: str
    method_id: str
    method_version: int
    semantics: tuple[str, ...]
    reused_policy_ids: tuple[str, ...]
    approved_data_ids: tuple[str, ...]
    provider_request_limit: int
    implemented_at: datetime
    policy_id: str
    canonical_hash: str
    authority: str = "NONE"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        implementation_id: str,
        selection: FirstGeneratedResearchMethodSelection,
        proposal: ResearchProposal,
        approval_id: str,
        approval_hash: str,
        reused_policy_ids: tuple[str, ...],
        implemented_at: datetime,
    ) -> FirstGeneratedResearchMethodImplementation:
        if (
            proposal.proposal_id != selection.selected_proposal_id
            or proposal.canonical_hash != selection.selected_proposal_hash
            or proposal.proposed_family != "PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION"
            or proposal.definition_kind != "REQUIRES_IMPLEMENTATION"
            or dict(proposal.experiment_configuration).get("implementation_scope")
            != "EXISTING_METHOD_OPERATIONAL_SEAM"
        ):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_IMPLEMENTATION_BINDING_INVALID")
        policies = tuple(sorted(set(reused_policy_ids)))
        required = {
            "SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1",
            "SHARED_OPPORTUNITY_PRIORITY_POLICY_V1",
            "TOTAL_MARKET_CONTEXT_POLICY_V1",
            "CROSS_VENUE_ALLOCATION_POLICY_V1",
        }
        if not required.issubset(policies):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_REUSED_POLICY_SET_INVALID")
        semantics = (
            "CONSUME_EXACT_RETAINED_CURRENT_OPPORTUNITIES",
            "PROJECT_UNCHANGED_SHARED_ELIGIBILITY_AND_PRIORITY",
            "ATTACH_UNCHANGED_TOTAL_MARKET_CONTEXT",
            "PROJECT_UNCHANGED_CROSS_VENUE_ALLOCATION_AND_PAPER_READINESS",
            "PRESERVE_MISSING_ECONOMICS_AND_ZERO_ALLOCATION",
            "NO_PROVIDER_REQUESTS_OR_FINANCIAL_MUTATION",
        )
        values = (
            _text(implementation_id, "FIRST_GENERATED_METHOD_IMPLEMENTATION_ID_INVALID", 300),
            selection.selection_id,
            selection.canonical_hash,
            proposal.proposal_id,
            proposal.canonical_hash,
            _text(approval_id, "FIRST_GENERATED_METHOD_APPROVAL_ID_INVALID", 300),
            _text(approval_hash, "FIRST_GENERATED_METHOD_APPROVAL_HASH_INVALID", 64),
            PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION_V1,
            1,
            semantics,
            policies,
            selection.approved_data_ids,
            0,
            _time(implemented_at),
            FIRST_GENERATED_RESEARCH_METHOD_IMPLEMENTATION_V1,
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class CurrentOpportunityOperationalEvidence:
    source_projection_ids: tuple[str, ...]
    source_projection_hashes: tuple[str, ...]
    shared_set_id: str
    shared_set_hash: str
    context_set_id: str
    context_set_hash: str
    allocation_id: str
    allocation_hash: str
    source_opportunity_count: int
    shared_eligible_count: int
    allocated_line_count: int
    paper_ready_proposal_ids: tuple[str, ...]
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.source_projection_ids) != len(self.source_projection_hashes):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_SOURCE_LINEAGE_INVALID")
        counts = (
            self.source_opportunity_count,
            self.shared_eligible_count,
            self.allocated_line_count,
        )
        if any(value < 0 for value in counts):
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_COUNT_INVALID")


@dataclass(frozen=True, slots=True)
class FirstGeneratedResearchMethodRun:
    method_run_id: str
    implementation_id: str
    implementation_hash: str
    proposal_id: str
    method_id: str
    source_projection_ids: tuple[str, ...]
    source_projection_hashes: tuple[str, ...]
    shared_set_id: str
    shared_set_hash: str
    context_set_id: str
    context_set_hash: str
    allocation_id: str
    allocation_hash: str
    source_opportunity_count: int
    shared_eligible_count: int
    allocated_line_count: int
    paper_ready_proposal_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    provider_request_count: int
    completed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        method_run_id: str,
        implementation: FirstGeneratedResearchMethodImplementation,
        evidence: CurrentOpportunityOperationalEvidence,
        completed_at: datetime,
    ) -> FirstGeneratedResearchMethodRun:
        blockers = tuple(sorted(set(evidence.blockers)))
        if evidence.shared_eligible_count == 0 and "ZERO_SHARED_ELIGIBLE" not in blockers:
            blockers += ("ZERO_SHARED_ELIGIBLE",)
        if not evidence.paper_ready_proposal_ids and "ZERO_PAPER_READY" not in blockers:
            blockers += ("ZERO_PAPER_READY",)
        values = (
            _text(method_run_id, "FIRST_GENERATED_METHOD_RUN_ID_INVALID", 300),
            implementation.implementation_id,
            implementation.canonical_hash,
            implementation.proposal_id,
            implementation.method_id,
            tuple(evidence.source_projection_ids),
            tuple(evidence.source_projection_hashes),
            _text(evidence.shared_set_id, "FIRST_GENERATED_METHOD_SHARED_ID_INVALID", 300),
            _text(evidence.shared_set_hash, "FIRST_GENERATED_METHOD_SHARED_HASH_INVALID", 64),
            _text(evidence.context_set_id, "FIRST_GENERATED_METHOD_CONTEXT_ID_INVALID", 300),
            _text(evidence.context_set_hash, "FIRST_GENERATED_METHOD_CONTEXT_HASH_INVALID", 64),
            _text(evidence.allocation_id, "FIRST_GENERATED_METHOD_ALLOCATION_ID_INVALID", 300),
            _text(evidence.allocation_hash, "FIRST_GENERATED_METHOD_ALLOCATION_HASH_INVALID", 64),
            evidence.source_opportunity_count,
            evidence.shared_eligible_count,
            evidence.allocated_line_count,
            tuple(sorted(set(evidence.paper_ready_proposal_ids))),
            tuple(sorted(set(blockers))),
            0,
            _time(completed_at),
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


def select_first_generated_proposal(
    proposals: tuple[ResearchProposal, ...],
    priorities: tuple[ResearchProposalPriority, ...],
) -> tuple[ResearchProposal, ResearchProposalPriority, tuple[tuple[str, str, str], ...]]:
    """Select one proposal without inventing parameters or changing rank order."""

    if len(proposals) != 3 or len(priorities) != 3:
        raise ResearchOperationsError("FIRST_GENERATED_METHOD_EXACTLY_THREE_PROPOSALS_REQUIRED")
    priority_by_proposal = {item.proposal_id: item for item in priorities}
    if set(priority_by_proposal) != {item.proposal_id for item in proposals}:
        raise ResearchOperationsError("FIRST_GENERATED_METHOD_PRIORITY_SET_INVALID")
    ordered = tuple(
        sorted(
            proposals, key=lambda item: priority_by_proposal[item.proposal_id].deterministic_rank
        )
    )
    if tuple(priority_by_proposal[item.proposal_id].deterministic_rank for item in ordered) != (
        1,
        2,
        3,
    ):
        raise ResearchOperationsError("FIRST_GENERATED_METHOD_PRIORITY_RANKS_INVALID")
    top = ordered[0]
    top_definition = dict(top.strategy_definition)
    precise_challenger = priority_by_proposal[
        top.proposal_id
    ].proposal_class is ResearchProposalClass.MATERIAL_CHALLENGER and {
        "entry_lookback",
        "exit_lookback",
        "entry_comparator",
        "exit_comparator",
    }.issubset(top_definition)
    if precise_challenger:
        selected = top
    else:
        operational = next(
            (
                item
                for item in ordered
                if priority_by_proposal[item.proposal_id].proposal_class
                is ResearchProposalClass.OPERATIONAL_METHOD
                and item.proposed_family == "PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION"
                and dict(item.experiment_configuration).get("implementation_scope")
                == "EXISTING_METHOD_OPERATIONAL_SEAM"
            ),
            None,
        )
        if operational is None:
            raise ResearchOperationsError("FIRST_GENERATED_METHOD_NO_IMPLEMENTATION_READY_PROPOSAL")
        selected = operational
    deferrals: list[tuple[str, str, str]] = []
    for item in ordered:
        if item.proposal_id == selected.proposal_id:
            continue
        if item is top and not precise_challenger:
            deferrals.append(
                (
                    item.proposal_id,
                    "MATERIAL_CHALLENGER_OMITS_EXACT_LOOKBACKS_AND_TRANSITION_THRESHOLDS",
                    "VERSIONED_PROPOSAL_WITH_PREDECLARED_LOOKBACKS_COMPARATORS_AND_TRANSITIONS",
                )
            )
        else:
            deferrals.append(
                (
                    item.proposal_id,
                    "LOWER_RANKED_OPERATIONAL_PROPOSAL_HAS_NO_EXACT_MATURE_METHOD_BINDING",
                    "EXACT_SOURCE_METHOD_AND_EXECUTABLE_RETAINED_EXPERIMENT_BINDING",
                )
            )
    return selected, priority_by_proposal[selected.proposal_id], tuple(deferrals)
