"""Governed research agenda, proposal, experiment, and recommendation orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.first_generated_research_method import (
    FirstGeneratedResearchMethodImplementation,
    FirstGeneratedResearchMethodRun,
    FirstGeneratedResearchMethodSelection,
)
from apex.domain.research_operations import (
    MAXIMUM_GENERATED_PROPOSALS,
    MAXIMUM_QUEUED_EXPERIMENTS,
    DuplicateDecision,
    PriorResearchKnowledge,
    ResearchAgendaItem,
    ResearchAgendaSourceFact,
    ResearchAgendaStatus,
    ResearchBudgetEvent,
    ResearchBudgetEventType,
    ResearchBudgetSummary,
    ResearchExperimentApproval,
    ResearchExperimentOutcome,
    ResearchExperimentQueueEvent,
    ResearchExperimentQueueItem,
    ResearchExperimentQueueStatus,
    ResearchFeasibilityState,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalClass,
    ResearchProposalFeasibility,
    ResearchProposalGeneratorRun,
    ResearchProposalOrigin,
    ResearchProposalPriority,
    ResearchProposalReview,
    ResearchProposalState,
    ResearchRecommendation,
    build_research_agenda_item,
    classify_research_proposal,
    generated_proposal_hypothesis,
    recommend_research_outcome,
    summarize_research_budget,
)
from apex.domain.values import content_digest


class ResearchOperationsRepository(Protocol):
    def agenda_item(self, agenda_item_id: str) -> ResearchAgendaItem | None: ...
    def agenda_items(self) -> tuple[ResearchAgendaItem, ...]: ...
    def save_agenda_item(self, value: ResearchAgendaItem) -> bool: ...
    def proposal(self, proposal_id: str) -> ResearchProposal | None: ...
    def proposals(self) -> tuple[ResearchProposal, ...]: ...
    def save_proposal(self, value: ResearchProposal) -> bool: ...
    def reviews(self, proposal_id: str) -> tuple[ResearchProposalReview, ...]: ...
    def save_review(self, value: ResearchProposalReview) -> bool: ...
    def budget_events(self, proposal_id: str) -> tuple[ResearchBudgetEvent, ...]: ...
    def save_budget_event(self, value: ResearchBudgetEvent) -> bool: ...
    def recommendation_for_run(self, run_id: str) -> ResearchRecommendation | None: ...
    def recommendations(self) -> tuple[ResearchRecommendation, ...]: ...
    def save_recommendation(self, value: ResearchRecommendation) -> bool: ...
    def generator_run(self, generator_run_id: str) -> ResearchProposalGeneratorRun | None: ...
    def generator_runs(self) -> tuple[ResearchProposalGeneratorRun, ...]: ...
    def save_generator_run(self, value: ResearchProposalGeneratorRun) -> bool: ...
    def proposal_priority(self, proposal_id: str) -> ResearchProposalPriority | None: ...
    def proposal_priorities(self) -> tuple[ResearchProposalPriority, ...]: ...
    def save_proposal_priority(self, value: ResearchProposalPriority) -> bool: ...
    def proposal_feasibility(self, proposal_id: str) -> ResearchProposalFeasibility | None: ...
    def proposal_feasibilities(self) -> tuple[ResearchProposalFeasibility, ...]: ...
    def save_proposal_feasibility(self, value: ResearchProposalFeasibility) -> bool: ...
    def experiment_approval(self, proposal_id: str) -> ResearchExperimentApproval | None: ...
    def save_experiment_approval(self, value: ResearchExperimentApproval) -> bool: ...
    def queue_item(self, queue_id: str) -> ResearchExperimentQueueItem | None: ...
    def queue_items(self) -> tuple[ResearchExperimentQueueItem, ...]: ...
    def save_queue_item(self, value: ResearchExperimentQueueItem) -> bool: ...
    def queue_events(self, queue_id: str) -> tuple[ResearchExperimentQueueEvent, ...]: ...
    def save_queue_event(self, value: ResearchExperimentQueueEvent) -> bool: ...
    def first_method_selection(self) -> FirstGeneratedResearchMethodSelection | None: ...
    def save_first_method_selection(self, value: FirstGeneratedResearchMethodSelection) -> bool: ...
    def first_method_implementation(self) -> FirstGeneratedResearchMethodImplementation | None: ...
    def save_first_method_implementation(
        self, value: FirstGeneratedResearchMethodImplementation
    ) -> bool: ...
    def first_method_run(self, method_run_id: str) -> FirstGeneratedResearchMethodRun | None: ...
    def first_method_runs(self) -> tuple[FirstGeneratedResearchMethodRun, ...]: ...
    def save_first_method_run(self, value: FirstGeneratedResearchMethodRun) -> bool: ...


class ResearchKnowledgeSource(Protocol):
    def knowledge(self) -> tuple[PriorResearchKnowledge, ...]: ...


class ResearchExperimentExecutor(Protocol):
    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome: ...


class InMemoryResearchOperationsRepository:
    def __init__(self) -> None:
        self._agenda: dict[str, ResearchAgendaItem] = {}
        self._proposals: dict[str, ResearchProposal] = {}
        self._reviews: dict[str, ResearchProposalReview] = {}
        self._budget: dict[str, ResearchBudgetEvent] = {}
        self._recommendations: dict[str, ResearchRecommendation] = {}
        self._generator_runs: dict[str, ResearchProposalGeneratorRun] = {}
        self._priorities: dict[str, ResearchProposalPriority] = {}
        self._feasibilities: dict[str, ResearchProposalFeasibility] = {}
        self._approvals: dict[str, ResearchExperimentApproval] = {}
        self._queue_items: dict[str, ResearchExperimentQueueItem] = {}
        self._queue_events: dict[str, ResearchExperimentQueueEvent] = {}
        self._first_method_selections: dict[str, FirstGeneratedResearchMethodSelection] = {}
        self._first_method_implementations: dict[
            str, FirstGeneratedResearchMethodImplementation
        ] = {}
        self._first_method_runs: dict[str, FirstGeneratedResearchMethodRun] = {}

    @staticmethod
    def _save[T](values: dict[str, T], identity: str, value: T) -> bool:
        retained = values.get(identity)
        if retained is not None:
            if retained != value:
                raise ResearchOperationsError("RESEARCH_OPERATIONS_IDENTITY_CONFLICT")
            return False
        values[identity] = value
        return True

    def agenda_item(self, agenda_item_id: str) -> ResearchAgendaItem | None:
        return self._agenda.get(agenda_item_id)

    def agenda_items(self) -> tuple[ResearchAgendaItem, ...]:
        return tuple(
            sorted(self._agenda.values(), key=lambda value: (-value.priority, value.agenda_item_id))
        )

    def save_agenda_item(self, value: ResearchAgendaItem) -> bool:
        return self._save(self._agenda, value.agenda_item_id, value)

    def proposal(self, proposal_id: str) -> ResearchProposal | None:
        return self._proposals.get(proposal_id)

    def proposals(self) -> tuple[ResearchProposal, ...]:
        return tuple(
            sorted(
                self._proposals.values(), key=lambda value: (value.created_at, value.proposal_id)
            )
        )

    def save_proposal(self, value: ResearchProposal) -> bool:
        return self._save(self._proposals, value.proposal_id, value)

    def reviews(self, proposal_id: str) -> tuple[ResearchProposalReview, ...]:
        return tuple(
            sorted(
                (value for value in self._reviews.values() if value.proposal_id == proposal_id),
                key=lambda value: value.ordinal,
            )
        )

    def save_review(self, value: ResearchProposalReview) -> bool:
        if any(
            item.ordinal == value.ordinal and item.review_id != value.review_id
            for item in self.reviews(value.proposal_id)
        ):
            raise ResearchOperationsError("RESEARCH_PROPOSAL_REVIEW_ORDINAL_CONFLICT")
        return self._save(self._reviews, value.review_id, value)

    def budget_events(self, proposal_id: str) -> tuple[ResearchBudgetEvent, ...]:
        return tuple(
            sorted(
                (value for value in self._budget.values() if value.proposal_id == proposal_id),
                key=lambda value: value.ordinal,
            )
        )

    def save_budget_event(self, value: ResearchBudgetEvent) -> bool:
        if any(
            item.ordinal == value.ordinal and item.event_id != value.event_id
            for item in self.budget_events(value.proposal_id)
        ):
            raise ResearchOperationsError("RESEARCH_BUDGET_EVENT_ORDINAL_CONFLICT")
        return self._save(self._budget, value.event_id, value)

    def recommendation_for_run(self, run_id: str) -> ResearchRecommendation | None:
        return next(
            (value for value in self._recommendations.values() if value.run_id == run_id), None
        )

    def recommendations(self) -> tuple[ResearchRecommendation, ...]:
        return tuple(
            sorted(
                self._recommendations.values(),
                key=lambda value: (value.created_at, value.recommendation_id),
            )
        )

    def save_recommendation(self, value: ResearchRecommendation) -> bool:
        return self._save(self._recommendations, value.recommendation_id, value)

    def generator_run(self, generator_run_id: str) -> ResearchProposalGeneratorRun | None:
        return self._generator_runs.get(generator_run_id)

    def generator_runs(self) -> tuple[ResearchProposalGeneratorRun, ...]:
        return tuple(sorted(self._generator_runs.values(), key=lambda value: value.generated_at))

    def save_generator_run(self, value: ResearchProposalGeneratorRun) -> bool:
        return self._save(self._generator_runs, value.generator_run_id, value)

    def proposal_priority(self, proposal_id: str) -> ResearchProposalPriority | None:
        return next(
            (value for value in self._priorities.values() if value.proposal_id == proposal_id), None
        )

    def proposal_priorities(self) -> tuple[ResearchProposalPriority, ...]:
        return tuple(
            sorted(
                self._priorities.values(),
                key=lambda value: (-value.total_score, value.tie_break_key),
            )
        )

    def save_proposal_priority(self, value: ResearchProposalPriority) -> bool:
        return self._save(self._priorities, value.priority_id, value)

    def proposal_feasibility(self, proposal_id: str) -> ResearchProposalFeasibility | None:
        values = tuple(
            value for value in self._feasibilities.values() if value.proposal_id == proposal_id
        )
        return max(
            values, key=lambda value: (value.evaluated_at, value.feasibility_id), default=None
        )

    def proposal_feasibilities(self) -> tuple[ResearchProposalFeasibility, ...]:
        return tuple(
            sorted(
                self._feasibilities.values(),
                key=lambda value: (value.proposal_id, value.evaluated_at, value.feasibility_id),
            )
        )

    def save_proposal_feasibility(self, value: ResearchProposalFeasibility) -> bool:
        return self._save(self._feasibilities, value.feasibility_id, value)

    def experiment_approval(self, proposal_id: str) -> ResearchExperimentApproval | None:
        return next(
            (value for value in self._approvals.values() if value.proposal_id == proposal_id), None
        )

    def save_experiment_approval(self, value: ResearchExperimentApproval) -> bool:
        return self._save(self._approvals, value.approval_id, value)

    def queue_item(self, queue_id: str) -> ResearchExperimentQueueItem | None:
        return self._queue_items.get(queue_id)

    def queue_items(self) -> tuple[ResearchExperimentQueueItem, ...]:
        return tuple(
            sorted(
                self._queue_items.values(),
                key=lambda value: (value.execution_order, value.queue_id),
            )
        )

    def save_queue_item(self, value: ResearchExperimentQueueItem) -> bool:
        return self._save(self._queue_items, value.queue_id, value)

    def queue_events(self, queue_id: str) -> tuple[ResearchExperimentQueueEvent, ...]:
        return tuple(
            sorted(
                (value for value in self._queue_events.values() if value.queue_id == queue_id),
                key=lambda value: value.ordinal,
            )
        )

    def save_queue_event(self, value: ResearchExperimentQueueEvent) -> bool:
        if any(
            item.ordinal == value.ordinal and item.event_id != value.event_id
            for item in self.queue_events(value.queue_id)
        ):
            raise ResearchOperationsError("RESEARCH_QUEUE_EVENT_ORDINAL_CONFLICT")
        return self._save(self._queue_events, value.event_id, value)

    def first_method_selection(self) -> FirstGeneratedResearchMethodSelection | None:
        return max(
            self._first_method_selections.values(),
            key=lambda value: value.selected_at,
            default=None,
        )

    def save_first_method_selection(self, value: FirstGeneratedResearchMethodSelection) -> bool:
        return self._save(self._first_method_selections, value.selection_id, value)

    def first_method_implementation(
        self,
    ) -> FirstGeneratedResearchMethodImplementation | None:
        return max(
            self._first_method_implementations.values(),
            key=lambda value: value.implemented_at,
            default=None,
        )

    def save_first_method_implementation(
        self, value: FirstGeneratedResearchMethodImplementation
    ) -> bool:
        return self._save(self._first_method_implementations, value.implementation_id, value)

    def first_method_run(self, method_run_id: str) -> FirstGeneratedResearchMethodRun | None:
        return self._first_method_runs.get(method_run_id)

    def first_method_runs(self) -> tuple[FirstGeneratedResearchMethodRun, ...]:
        return tuple(
            sorted(
                self._first_method_runs.values(),
                key=lambda value: (value.completed_at, value.method_run_id),
            )
        )

    def save_first_method_run(self, value: FirstGeneratedResearchMethodRun) -> bool:
        return self._save(self._first_method_runs, value.method_run_id, value)


class EmptyResearchKnowledgeSource:
    def knowledge(self) -> tuple[PriorResearchKnowledge, ...]:
        return ()


def proposal_state(
    repository: ResearchOperationsRepository, proposal_id: str
) -> ResearchProposalState:
    reviews = repository.reviews(proposal_id)
    return reviews[-1].to_state if reviews else ResearchProposalState.DRAFT


def _proposal_knowledge(
    repository: ResearchOperationsRepository,
) -> tuple[PriorResearchKnowledge, ...]:
    values: list[PriorResearchKnowledge] = []
    for proposal in repository.proposals():
        state = proposal_state(repository, proposal.proposal_id)
        values.append(
            PriorResearchKnowledge(
                research_id=proposal.proposal_id,
                venue=proposal.venue,
                proposed_family=proposal.proposed_family,
                semantic_hash=proposal.semantic_hash,
                experiment_configuration_hash=proposal.experiment_configuration_hash,
                validation_windows=proposal.validation_windows,
                rejected=state
                in {
                    ResearchProposalState.REJECTED,
                    ResearchProposalState.DUPLICATE,
                    ResearchProposalState.INVALID,
                },
                canonical_hash=proposal.canonical_hash,
            )
        )
    return tuple(values)


def duplicate_decision(
    repository: ResearchOperationsRepository,
    proposal: ResearchProposal,
    knowledge_source: ResearchKnowledgeSource,
) -> DuplicateDecision:
    prior = tuple(
        value
        for value in (*knowledge_source.knowledge(), *_proposal_knowledge(repository))
        if value.research_id != proposal.proposal_id
    )
    return classify_research_proposal(proposal, prior)


@dataclass(frozen=True, slots=True)
class AgendaRefreshResult:
    items: tuple[ResearchAgendaItem, ...]
    replayed_item_ids: tuple[str, ...]


class RefreshResearchAgenda:
    def __init__(self, repository: ResearchOperationsRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        facts: tuple[ResearchAgendaSourceFact, ...],
        related_research: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> AgendaRefreshResult:
        related = dict(related_research)
        items: list[ResearchAgendaItem] = []
        replayed: list[str] = []
        for fact in sorted(facts, key=lambda value: (value.observed_at, value.fact_id)):
            item = build_research_agenda_item(
                fact,
                prior_related_research=related.get(fact.fact_id, ()),
            )
            if not self._repository.save_agenda_item(item):
                replayed.append(item.agenda_item_id)
            items.append(item)
        return AgendaRefreshResult(tuple(items), tuple(replayed))


class CreateResearchProposal:
    def __init__(self, repository: ResearchOperationsRepository) -> None:
        self._repository = repository

    def execute(self, proposal: ResearchProposal) -> tuple[ResearchProposal, bool]:
        if self._repository.agenda_item(proposal.agenda_item_id) is None:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_AGENDA_ITEM_NOT_FOUND")
        created = self._repository.save_proposal(proposal)
        return proposal, not created


@dataclass(frozen=True, slots=True)
class ResearchProposalGenerationContext:
    retained_data_ids: tuple[str, ...]
    executable_experiment_ids: tuple[str, ...]
    provider_capability_ids: tuple[str, ...]
    remaining_experiment_budget: int
    evidence_class_compatible: bool = True
    # Counterfactual V2 Research Evidence Quarantine v1. Source fact ids
    # whose already-retained agenda items must not originate a *new*
    # proposal. Deliberately expressed as opaque ``source_fact_id`` strings
    # rather than as anything evidence-specific: this module stays entirely
    # agnostic about *why* a caller excludes a fact (which venue, which
    # policy version, which defect), exactly as it is already agnostic
    # about why a data id is or is not in ``retained_data_ids``. The
    # adapter that knows the evidence semantics -- ``adapters.research_
    # operations_sources.RetainedResearchAgendaSource`` -- decides; this
    # module only honours the decision.
    #
    # Necessary as well as, not instead of, withholding the source fact:
    # ``execute`` below selects candidates from the *retained agenda item
    # table*, never from this cycle's facts, so an agenda item created
    # before a defect was known stays a permanently eligible candidate no
    # matter what the fact source does afterwards. Excluding the item is
    # never the same as closing it: its status is untouched and it remains
    # exactly as retained.
    excluded_source_fact_ids: tuple[str, ...] = ()


class AIResearchProposalProvider(Protocol):
    enabled: bool

    def propose(
        self,
        *,
        agenda: tuple[ResearchAgendaItem, ...],
        prior_research: tuple[PriorResearchKnowledge, ...],
        supported_strategy_grammar: tuple[str, ...],
        allowed_data_ids: tuple[str, ...],
        policy_id: str,
    ) -> tuple[ResearchProposal, ...]: ...


class DisabledAIResearchProposalProvider:
    enabled = False

    def propose(
        self,
        *,
        agenda: tuple[ResearchAgendaItem, ...],
        prior_research: tuple[PriorResearchKnowledge, ...],
        supported_strategy_grammar: tuple[str, ...],
        allowed_data_ids: tuple[str, ...],
        policy_id: str,
    ) -> tuple[ResearchProposal, ...]:
        del agenda, prior_research, supported_strategy_grammar, allowed_data_ids, policy_id
        raise ResearchOperationsError("RESEARCH_AI_PROPOSAL_PROVIDER_DISABLED")


@dataclass(frozen=True, slots=True)
class GeneratedResearchProposals:
    generator_run: ResearchProposalGeneratorRun
    proposals: tuple[ResearchProposal, ...]
    priorities: tuple[ResearchProposalPriority, ...]
    feasibilities: tuple[ResearchProposalFeasibility, ...]
    idempotent_replay: bool


type ProposalTemplate = tuple[
    ResearchProposalClass,
    str,
    str,
    tuple[tuple[str, str], ...],
    str,
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    bool,
    bool,
]


# Research Experiment Executability v1: these two families are the exact
# ``proposed_family`` suffixes ``_agenda_template`` mints below for
# ``COUNTERFACTUAL_OUTCOME``/``SECURITIES_STRUCTURE_OUTCOME`` facts. Both are
# declared ``EXISTING_RESEARCH_REPLICATION`` proposals whose own
# ``experiment_configuration`` already says "scope: RETAINED_..._AGGREGATE_
# ONLY" -- they never need a ``research_integrity`` experiment family (that
# module's families are a structurally separate pre-registration concept for
# freezing NEW research degrees of freedom, and none has ever existed for
# any CRYPTO/KALSHI/SECURITIES method id). ``GenerateResearchProposals``
# below and ``GovernedResearchExecutor``
# (``adapters.research_operations_executor``) both key off these exact
# suffixes so a proposal that declares this scope is provably routed to the
# one executor that honors it, never the generic ledger executor.
COUNTERFACTUAL_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX = "_COUNTERFACTUAL_OUTCOME_POLICY_REVIEW"
SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX = (
    "_SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW"
)
RETAINED_AGGREGATE_ONLY_FAMILY_SUFFIXES = (
    COUNTERFACTUAL_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX,
    SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX,
)


def _agenda_template(item: ResearchAgendaItem) -> ProposalTemplate:
    identity = item.source_fact_id.upper().replace("-", "_")
    scope = item.market_scope[0] if item.market_scope else item.venue.value
    if "INSUFFICIENT_CALIBRATION" in identity or "FAILED_HEALTH" in identity:
        return (
            ResearchProposalClass.REPLICATION,
            f"{scope}_FROZEN_SEMANTICS_REPLICATION",
            "EXISTING_RESEARCH_REPLICATION",
            (("semantics", "FROZEN"), ("execution", "NEXT_OPEN")),
            "DISJOINT_SCOPE_REPLICATION_WITH_FROZEN_SEMANTICS",
            (item.source_fact_id,),
            (("existing_experiment_id", scope), ("scope", "DISJOINT_RETAINED_SCOPE")),
            True,
            True,
        )
    if "NEGATIVE_EXPECTED_ECONOMICS" in identity:
        return (
            ResearchProposalClass.MATERIAL_CHALLENGER,
            f"{scope}_MATERIAL_STATEFUL_CHALLENGER",
            "CRYPTO_GENERIC_LONG_CASH",
            (
                ("feature", "POINT_TO_POINT_RETURN"),
                ("decision_cadence", "CALENDAR_MONTHLY"),
                ("state", "STATEFUL_LONG_CASH"),
                ("execution", "NEXT_OPEN"),
            ),
            "STATEFUL_MONTHLY_REGIME_TRANSITIONS_REPLACE_SHORT_HORIZON_REVERSAL_RANKING",
            (item.source_fact_id,),
            (("existing_experiment_id", scope), ("scope", "RETAINED_DEVELOPMENT_ONLY")),
            True,
            True,
        )
    if "ZERO_SHARED_ELIGIBLE" in identity:
        return (
            ResearchProposalClass.OPERATIONAL_METHOD,
            "SHARED_OPPORTUNITY_EXISTING_METHOD_OPERATIONALIZATION",
            "EXISTING_RESEARCH_REPLICATION",
            (("operation", "PROJECT_EXISTING_METHOD_TO_SHARED_OPPORTUNITY"),),
            "CONNECT_MATURE_RETAINED_METHOD_WITHOUT_CHANGING_ALGORITHM_OR_POLICY",
            (item.source_fact_id,),
            (("existing_experiment_id", scope), ("scope", "CURRENT_RETAINED_SET")),
            True,
            True,
        )
    if "ZERO_PAPER_READY" in identity:
        return (
            ResearchProposalClass.OPERATIONAL_METHOD,
            "PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION",
            "REQUIRES_IMPLEMENTATION",
            (("unsupported_requirement", "NEW_OPERATIONAL_INTEGRATION"),),
            "CONNECT_EXISTING_RESEARCH_TO_CURRENT_OPPORTUNITY_SEAM_WITHOUT_NEW_ALGORITHM",
            (item.source_fact_id,),
            (("implementation_scope", "EXISTING_METHOD_OPERATIONAL_SEAM"),),
            False,
            True,
        )
    if "COUNTERFACTUAL_OUTCOME" in identity:
        # Counterfactual Outcome Feedback v1: a qualified aggregate over
        # rejected-decision follow-up evidence is a prompt for a governed
        # human-reviewed *replication/re-examination* of whether the
        # current eligibility/mandate rejection policy remains well-
        # calibrated for this exact reason-family/venue/method cohort --
        # never an automatic parameter change, ranking input, or
        # allocation/mandate mutation (none of which this proposal
        # template, or anything downstream of it, can grant: ``paper_
        # eligible``/``live_eligible`` stay hard-pinned ``False`` on every
        # ``ResearchProposal`` regardless of class).
        return (
            ResearchProposalClass.REPLICATION,
            f"{scope}{COUNTERFACTUAL_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX}",
            "EXISTING_RESEARCH_REPLICATION",
            (("semantics", "FROZEN"), ("execution", "RETAINED_COUNTERFACTUAL_EVIDENCE_ONLY")),
            "REVIEW_WHETHER_CURRENT_REJECTION_POLICY_REMAINS_CALIBRATED_GIVEN_QUALIFIED_"
            "AGGREGATE_OUTCOME_EVIDENCE",
            (item.source_fact_id,),
            (
                ("existing_experiment_id", scope),
                ("scope", "RETAINED_COUNTERFACTUAL_AGGREGATE_ONLY"),
            ),
            True,
            True,
        )
    if "SECURITIES_STRUCTURE_OUTCOME" in identity:
        # Securities Structure Forward Performance v1: a qualified aggregate
        # over realized options-structure payoffs is a prompt for a
        # governed human-reviewed *replication/re-examination* of whether
        # the current structure-selection policy remains well-calibrated
        # for this exact method/structure-type cohort -- never an automatic
        # parameter change, ranking input, or universe mutation (none of
        # which this proposal template, or anything downstream of it, can
        # grant: ``paper_eligible``/``live_eligible`` stay hard-pinned
        # ``False`` on every ``ResearchProposal`` regardless of class).
        # Mirrors the ``COUNTERFACTUAL_OUTCOME`` branch above exactly --
        # before this branch existed, every ``SECURITIES_STRUCTURE_OUTCOME``
        # fact fell through to the generic ``EVIDENCE_COMPLETION`` fallback
        # below, which is both semantically wrong (that template means
        # "evidence is missing", not "a mature qualified pattern exists")
        # and the lowest-scoring proposal class -- see
        # ``docs/EVIDENCE_AND_FEEDBACK_LIFECYCLE.md``.
        return (
            ResearchProposalClass.REPLICATION,
            f"{scope}{SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW_FAMILY_SUFFIX}",
            "EXISTING_RESEARCH_REPLICATION",
            (("semantics", "FROZEN"), ("execution", "RETAINED_STRUCTURE_OUTCOME_EVIDENCE_ONLY")),
            "REVIEW_WHETHER_CURRENT_STRUCTURE_SELECTION_POLICY_REMAINS_CALIBRATED_GIVEN_"
            "QUALIFIED_AGGREGATE_OUTCOME_EVIDENCE",
            (item.source_fact_id,),
            (
                ("existing_experiment_id", scope),
                ("scope", "RETAINED_STRUCTURE_OUTCOME_AGGREGATE_ONLY"),
            ),
            True,
            True,
        )
    missing = (
        "MISSING:COMPATIBLE_COMPLETED_OUTCOMES"
        if "NON_POSITIVE_NET_GAP" in identity
        else "MISSING:EXACT_RULE_AND_RELATIONSHIP_EVIDENCE"
        if "EXHAUSTIVENESS_UNPROVEN" in identity
        else "MISSING:QUALITY_COMPLETE_RETAINED_DATA"
    )
    return (
        ResearchProposalClass.EVIDENCE_COMPLETION,
        f"{scope}_EVIDENCE_COMPLETION",
        (
            "KALSHI_RETAINED_RELATIONSHIP_METHOD"
            if item.venue.value == "KALSHI"
            else "CRYPTO_CURRENT_METHOD"
        ),
        (("semantics", "UNCHANGED"), ("required_capture", missing)),
        "COMPLETE_EXACT_MISSING_EVIDENCE_WITHOUT_CHANGING_METHOD_SEMANTICS",
        (item.source_fact_id, missing),
        (("bounded_capture", "RETAINED_PUBLIC_EVIDENCE_ONLY"),),
        True,
        False,
    )


def _priority_components(
    item: ResearchAgendaItem, proposal_class: ResearchProposalClass
) -> tuple[int, int, int, int, int, int, int]:
    evidence = {
        ResearchProposalClass.REPLICATION: 85,
        ResearchProposalClass.MATERIAL_CHALLENGER: 70,
        ResearchProposalClass.OPERATIONAL_METHOD: 75,
        ResearchProposalClass.EVIDENCE_COMPLETION: 45,
    }[proposal_class]
    novelty = {
        ResearchProposalClass.REPLICATION: 55,
        ResearchProposalClass.MATERIAL_CHALLENGER: 90,
        ResearchProposalClass.OPERATIONAL_METHOD: 70,
        ResearchProposalClass.EVIDENCE_COMPLETION: 60,
    }[proposal_class]
    execution_cost = 80 if proposal_class is not ResearchProposalClass.EVIDENCE_COMPLETION else 45
    runtime = 85 if proposal_class is ResearchProposalClass.OPERATIONAL_METHOD else 70
    decision_value = min(100, item.priority + 10)
    coverage = 90 if proposal_class is ResearchProposalClass.OPERATIONAL_METHOD else 65
    return (item.priority, evidence, novelty, execution_cost, runtime, decision_value, coverage)


def _priority_total(components: tuple[int, int, int, int, int, int, int]) -> int:
    return (
        components[0] * 30
        + components[1] * 20
        + components[2] * 15
        + components[3] * 10
        + components[4] * 5
        + components[5] * 15
        + components[6] * 5
    ) // 100


class GenerateResearchProposals:
    """Deterministic bounded generator over exact retained agenda evidence only."""

    def __init__(
        self,
        repository: ResearchOperationsRepository,
        knowledge_source: ResearchKnowledgeSource | None = None,
        ai_provider: AIResearchProposalProvider | None = None,
    ) -> None:
        self._repository = repository
        self._knowledge = knowledge_source or EmptyResearchKnowledgeSource()
        self._ai_provider = ai_provider or DisabledAIResearchProposalProvider()

    def execute(
        self,
        *,
        generator_run_id: str,
        generated_at: datetime,
        actor: str,
        context: ResearchProposalGenerationContext,
        maximum_proposals: int = MAXIMUM_GENERATED_PROPOSALS,
        origin: ResearchProposalOrigin = ResearchProposalOrigin.SYSTEM_HEURISTIC,
    ) -> GeneratedResearchProposals:
        if not 1 <= maximum_proposals <= MAXIMUM_GENERATED_PROPOSALS:
            raise ResearchOperationsError("RESEARCH_GENERATOR_LIMIT_INVALID")
        retained_run = self._repository.generator_run(generator_run_id)
        if retained_run is not None:
            retained_proposals = tuple(
                proposal_value
                for identity in retained_run.generated_proposal_ids
                if (proposal_value := self._repository.proposal(identity)) is not None
            )
            return GeneratedResearchProposals(
                retained_run,
                retained_proposals,
                tuple(
                    priority_value
                    for proposal in retained_proposals
                    if (priority_value := self._repository.proposal_priority(proposal.proposal_id))
                    is not None
                ),
                tuple(
                    feasibility_value
                    for proposal in retained_proposals
                    if (
                        feasibility_value := self._repository.proposal_feasibility(
                            proposal.proposal_id
                        )
                    )
                    is not None
                ),
                True,
            )
        if origin is ResearchProposalOrigin.AI_ASSISTED:
            if not self._ai_provider.enabled:
                raise ResearchOperationsError("RESEARCH_AI_PROPOSAL_PROVIDER_DISABLED")
            raise ResearchOperationsError("RESEARCH_AI_EXTERNAL_MODEL_CALLS_DISABLED")
        proposed_agenda_ids = {value.agenda_item_id for value in self._repository.proposals()}
        excluded_source_fact_ids = frozenset(context.excluded_source_fact_ids)
        agenda = tuple(
            value
            for value in self._repository.agenda_items()
            if value.status.value in {"OPEN", "DEFERRED"}
            and value.agenda_item_id not in proposed_agenda_ids
            and value.source_fact_id not in excluded_source_fact_ids
        )
        candidates: list[
            tuple[
                ResearchAgendaItem,
                ResearchProposalClass,
                ProposalTemplate,
                tuple[int, int, int, int, int, int, int],
            ]
        ] = []
        for item in agenda:
            template = _agenda_template(item)
            proposal_class = template[0]
            candidates.append(
                (item, proposal_class, template, _priority_components(item, proposal_class))
            )
        candidates.sort(
            key=lambda value: (
                -_priority_total(value[3]),
                value[0].agenda_item_id,
            )
        )
        selected: list[
            tuple[
                ResearchAgendaItem,
                ResearchProposalClass,
                ProposalTemplate,
                tuple[int, int, int, int, int, int, int],
            ]
        ] = []
        families: set[str] = set()
        # Research Proposal Coverage & Fairness v1: reserve exactly one of
        # the batch's slots (when the cap allows more than one proposal at
        # all) for the fairness pick below, so a family that structurally
        # scores lower than another, continuously-replenishing family is
        # never crowded out of *every* slot by fresh, higher-scoring
        # candidates every single cycle -- see ``_priority_components``:
        # every fact type this function does not explicitly recognize
        # (every ``KALSHI`` fact type, still, even after this milestone)
        # falls into the generic, lowest-scoring ``EVIDENCE_COMPLETION``
        # class, exactly the class ``SECURITIES_STRUCTURE_OUTCOME`` facts
        # were also wrongly falling into before this milestone's other,
        # separate fix above (the two fixes are independent: this one
        # guarantees a starved family still eventually wins a slot even
        # when it is correctly classed and simply outscored). Reserving
        # only when ``maximum_proposals > 1`` keeps the exact original
        # behavior for a single-slot batch -- there is no slot to spare.
        priority_cap = maximum_proposals - 1 if maximum_proposals > 1 else maximum_proposals
        for candidate in candidates:
            family = str(candidate[2][1])
            if family in families:
                continue
            selected.append(candidate)
            families.add(family)
            if len(selected) == priority_cap:
                break
        if len(selected) < maximum_proposals:
            unrepresented = [
                candidate for candidate in candidates if str(candidate[2][1]) not in families
            ]
            if unrepresented:
                oldest = min(
                    unrepresented,
                    key=lambda candidate: (candidate[0].created_at, candidate[0].agenda_item_id),
                )
                selected.append(oldest)
                families.add(str(oldest[2][1]))
        proposals: list[ResearchProposal] = []
        priorities: list[ResearchProposalPriority] = []
        feasibilities: list[ResearchProposalFeasibility] = []
        for rank, (item, proposal_class, template, components) in enumerate(selected, start=1):
            (
                _,
                family,
                definition_kind,
                strategy_definition,
                exact_change,
                required_data,
                configuration,
                engine_compatible,
                provider_available,
            ) = template
            raw_id = f"generated-{generator_run_id}-{item.agenda_item_id}"
            proposal_id = (
                raw_id if len(raw_id) <= 220 else f"{raw_id[:155]}-{content_digest(raw_id)}"
            )
            proposal = ResearchProposal.create(
                proposal_id=proposal_id,
                version=1,
                agenda_item_id=item.agenda_item_id,
                venue=item.venue,
                hypothesis=generated_proposal_hypothesis(proposal_class, item.problem_statement),
                expected_mechanism=str(exact_change).replace("_", " ").title(),
                instruments=item.market_scope or (item.venue.value,),
                proposed_family=str(family),
                definition_kind=str(definition_kind),
                strategy_definition=strategy_definition,
                exact_change_from_prior=str(exact_change),
                prior_research_ids=item.prior_related_research,
                required_data_ids=required_data,
                experiment_configuration=configuration,
                validation_windows=(("scope", "EXACT_RETAINED_OR_DISJOINT_SCOPE"),),
                cost_assumptions=(("cost_policy", "EXISTING_FIXED_COST_ASSUMPTIONS"),),
                success_criteria=("EXACT_COMPLETION_CONDITION_MET", "DECISION_EVIDENCE_IMPROVED"),
                failure_criteria=("COMPLETION_CONDITION_NOT_MET", "ECONOMICS_OR_QUALITY_FAILED"),
                maximum_experiment_runs=1,
                origin=origin,
                actor=actor,
                warnings=(
                    "RESEARCH_ONLY",
                    "NO_AUTOMATIC_APPROVAL",
                    "NO_PAPER_OR_LIVE_AUTHORITY",
                ),
                created_at=generated_at,
            )
            self._repository.save_proposal(proposal)
            priority = ResearchProposalPriority.create(
                generator_run_id=generator_run_id,
                proposal=proposal,
                proposal_class=proposal_class,
                importance_score=components[0],
                evidence_score=components[1],
                novelty_score=components[2],
                execution_cost_score=components[3],
                runtime_score=components[4],
                decision_value_score=components[5],
                library_coverage_score=components[6],
                deterministic_rank=rank,
            )
            self._repository.save_proposal_priority(priority)
            duplicate = duplicate_decision(self._repository, proposal, self._knowledge)
            data_available = set(proposal.required_data_ids).issubset(context.retained_data_ids)
            exact_experiment_id = dict(proposal.experiment_configuration).get(
                "existing_experiment_id"
            )
            compatible = bool(
                engine_compatible
                and (
                    proposal_class is ResearchProposalClass.EVIDENCE_COMPLETION
                    or str(family).endswith(RETAINED_AGGREGATE_ONLY_FAMILY_SUFFIXES)
                    or (
                        exact_experiment_id
                        and exact_experiment_id in context.executable_experiment_ids
                    )
                )
            )
            feasibility = ResearchProposalFeasibility.create(
                generator_run_id=generator_run_id,
                proposal=proposal,
                duplicate_decision=duplicate,
                schema_valid=True,
                data_available=data_available,
                provider_capability_available=bool(provider_available),
                experiment_engine_compatible=compatible,
                budget_available=context.remaining_experiment_budget > 0,
                evidence_class_compatible=context.evidence_class_compatible,
                authority_restrictions_passed=(
                    proposal.authority == "NONE"
                    and not proposal.paper_eligible
                    and not proposal.live_eligible
                ),
                evaluated_at=generated_at,
            )
            self._repository.save_proposal_feasibility(feasibility)
            proposals.append(proposal)
            priorities.append(priority)
            feasibilities.append(feasibility)
        generator_run = ResearchProposalGeneratorRun.create(
            generator_run_id=generator_run_id,
            agenda_items=agenda,
            generated_proposal_ids=tuple(value.proposal_id for value in proposals),
            maximum_proposals=maximum_proposals,
            origin=origin,
            actor=actor,
            external_model_called=False,
            generated_at=generated_at,
        )
        self._repository.save_generator_run(generator_run)
        return GeneratedResearchProposals(
            generator_run,
            tuple(proposals),
            tuple(priorities),
            tuple(feasibilities),
            False,
        )


class ReviewResearchProposal:
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
        proposal_id: str,
        target_state: ResearchProposalState,
        actor: str,
        reason: str,
        confirmation: str,
        reviewed_at: datetime,
        idempotency_key: str | None = None,
        approved_experiment_id: str | None = None,
        approved_data_ids: tuple[str, ...] | None = None,
        implementation_scope: str | None = None,
        provider_request_limit: int = 0,
        deferred_proposal_ids: tuple[str, ...] = (),
    ) -> tuple[ResearchProposalReview, DuplicateDecision, bool]:
        proposal = self._repository.proposal(proposal_id)
        if proposal is None:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_NOT_FOUND")
        decision = duplicate_decision(self._repository, proposal, self._knowledge)
        retained_reviews = self._repository.reviews(proposal_id)
        if retained_reviews:
            retained = retained_reviews[-1]
            if (
                retained.to_state is target_state
                and retained.actor == actor
                and retained.reason == reason
                and retained.confirmation == confirmation
                and retained.duplicate_decision_hash == decision.canonical_hash
            ):
                if target_state is ResearchProposalState.APPROVED_FOR_EXPERIMENT:
                    approval = self._repository.experiment_approval(proposal_id)
                    if approval is None or approval.idempotency_key != idempotency_key:
                        raise ResearchOperationsError(
                            "RESEARCH_EXPERIMENT_APPROVAL_IDEMPOTENCY_CONFLICT"
                        )
                return retained, decision, True
        current = proposal_state(self._repository, proposal_id)
        if target_state is ResearchProposalState.APPROVED_FOR_EXPERIMENT and not actor.startswith(
            "operator:"
        ):
            raise ResearchOperationsError("RESEARCH_PROPOSAL_OPERATOR_APPROVAL_REQUIRED")
        if target_state is ResearchProposalState.APPROVED_FOR_EXPERIMENT and not idempotency_key:
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_APPROVAL_IDEMPOTENCY_KEY_REQUIRED")
        if target_state is ResearchProposalState.DUPLICATE and decision.approval_permitted:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_DUPLICATE_CLASSIFICATION_MISMATCH")
        ordinal = len(retained_reviews) + 1
        review = ResearchProposalReview.create(
            review_id=f"{proposal_id}-review-{ordinal}",
            proposal_id=proposal_id,
            ordinal=ordinal,
            from_state=current,
            to_state=target_state,
            duplicate_decision=decision,
            actor=actor,
            reason=reason,
            confirmation=confirmation,
            reviewed_at=reviewed_at,
        )
        created = self._repository.save_review(review)
        if target_state is ResearchProposalState.APPROVED_FOR_EXPERIMENT:
            self._repository.save_experiment_approval(
                ResearchExperimentApproval.create(
                    proposal=proposal,
                    review=review,
                    idempotency_key=idempotency_key or "",
                    approved_experiment_id=approved_experiment_id,
                    approved_data_ids=approved_data_ids,
                    implementation_scope=implementation_scope,
                    provider_request_limit=provider_request_limit,
                    deferred_proposal_ids=deferred_proposal_ids,
                )
            )
            budget = ResearchBudgetEvent.create(
                event_id=f"{proposal_id}-budget-approval",
                proposal_id=proposal_id,
                run_id=f"{proposal_id}-approval",
                ordinal=len(self._repository.budget_events(proposal_id)) + 1,
                event_type=ResearchBudgetEventType.APPROVAL_RESERVED,
                reason="PROPOSAL_APPROVED_BUT_NO_EXPERIMENT_RUN_CONSUMED",
                occurred_at=reviewed_at,
            )
            self._repository.save_budget_event(budget)
        return review, decision, not created


@dataclass(frozen=True, slots=True)
class GovernedExperimentResult:
    outcome: ResearchExperimentOutcome
    recommendation: ResearchRecommendation
    budget: ResearchBudgetSummary
    idempotent_replay: bool


class RunGovernedResearchExperiment:
    def __init__(
        self,
        repository: ResearchOperationsRepository,
        executor: ResearchExperimentExecutor,
        knowledge_source: ResearchKnowledgeSource | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._knowledge = knowledge_source or EmptyResearchKnowledgeSource()

    def execute(
        self,
        *,
        proposal_id: str,
        run_id: str,
        actor: str,
        confirmation: str,
        executed_at: datetime,
    ) -> GovernedExperimentResult:
        proposal = self._repository.proposal(proposal_id)
        if proposal is None:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_NOT_FOUND")
        if (
            not actor.startswith("operator:")
            or confirmation != f"RUN:{proposal_id}:{run_id}:{proposal.canonical_hash}"
        ):
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_CONFIRMATION_INVALID")
        retained = self._repository.recommendation_for_run(run_id)
        if retained is not None:
            outcome = self._executor.execute(
                proposal=proposal,
                run_id=run_id,
                executed_at=retained.created_at,
            )
            if retained.outcome_hash != outcome.canonical_hash:
                raise ResearchOperationsError("RESEARCH_EXPERIMENT_REPLAY_DIVERGENCE")
            retained_events = self._repository.budget_events(proposal_id)
            retained_replay = next(
                (
                    value
                    for value in retained_events
                    if value.run_id == run_id and value.event_type is ResearchBudgetEventType.REPLAY
                ),
                None,
            )
            if retained_replay is None:
                replay_event = ResearchBudgetEvent.create(
                    event_id=f"{proposal_id}-budget-{run_id}-replay",
                    proposal_id=proposal_id,
                    run_id=run_id,
                    ordinal=len(retained_events) + 1,
                    event_type=ResearchBudgetEventType.REPLAY,
                    reason="EXACT_RUN_REPLAY_DOES_NOT_CONSUME_BUDGET",
                    occurred_at=executed_at,
                )
                self._repository.save_budget_event(replay_event)
            return GovernedExperimentResult(
                outcome,
                retained,
                summarize_research_budget(proposal, self._repository.budget_events(proposal_id)),
                True,
            )
        if (
            proposal_state(self._repository, proposal_id)
            is not ResearchProposalState.APPROVED_FOR_EXPERIMENT
        ):
            raise ResearchOperationsError("RESEARCH_PROPOSAL_NOT_APPROVED")
        budget = summarize_research_budget(proposal, self._repository.budget_events(proposal_id))
        if budget.remaining_runs <= 0:
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_BUDGET_EXHAUSTED")
        decision = duplicate_decision(self._repository, proposal, self._knowledge)
        ordinal = len(self._repository.reviews(proposal_id)) + 1
        running = ResearchProposalReview.create(
            review_id=f"{proposal_id}-review-{ordinal}",
            proposal_id=proposal_id,
            ordinal=ordinal,
            from_state=ResearchProposalState.APPROVED_FOR_EXPERIMENT,
            to_state=ResearchProposalState.RUNNING,
            duplicate_decision=decision,
            actor=actor,
            reason="EXACT_APPROVED_EXPERIMENT_STARTED",
            confirmation=f"RUNNING:{proposal_id}:{decision.canonical_hash}",
            reviewed_at=executed_at,
        )
        self._repository.save_review(running)
        outcome = self._executor.execute(proposal=proposal, run_id=run_id, executed_at=executed_at)
        if outcome.proposal_id != proposal_id or outcome.run_id != run_id:
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_OUTCOME_IDENTITY_MISMATCH")
        budget_event = ResearchBudgetEvent.create(
            event_id=f"{proposal_id}-budget-{run_id}",
            proposal_id=proposal_id,
            run_id=run_id,
            ordinal=len(self._repository.budget_events(proposal_id)) + 1,
            event_type=(
                ResearchBudgetEventType.EXPERIMENT_RUN
                if outcome.completed
                else ResearchBudgetEventType.FAILED_RUN
            ),
            reason="BOUNDED_EXISTING_EXPERIMENT_EXECUTOR_RESULT_RETAINED",
            occurred_at=executed_at,
        )
        self._repository.save_budget_event(budget_event)
        recommendation = recommend_research_outcome(outcome)
        self._repository.save_recommendation(recommendation)
        complete_ordinal = len(self._repository.reviews(proposal_id)) + 1
        completed = ResearchProposalReview.create(
            review_id=f"{proposal_id}-review-{complete_ordinal}",
            proposal_id=proposal_id,
            ordinal=complete_ordinal,
            from_state=ResearchProposalState.RUNNING,
            to_state=ResearchProposalState.COMPLETED,
            duplicate_decision=decision,
            actor=actor,
            reason=f"RECOMMENDATION_{recommendation.state.value}",
            confirmation=f"COMPLETED:{proposal_id}:{decision.canonical_hash}",
            reviewed_at=executed_at,
        )
        self._repository.save_review(completed)
        return GovernedExperimentResult(
            outcome,
            recommendation,
            summarize_research_budget(proposal, self._repository.budget_events(proposal_id)),
            False,
        )


def experiment_queue_status(
    repository: ResearchOperationsRepository, queue_id: str
) -> ResearchExperimentQueueStatus:
    events = repository.queue_events(queue_id)
    if not events:
        raise ResearchOperationsError("RESEARCH_QUEUE_EVENT_NOT_FOUND")
    return events[-1].to_status


@dataclass(frozen=True, slots=True)
class EnqueuedResearchExperiment:
    item: ResearchExperimentQueueItem
    event: ResearchExperimentQueueEvent
    idempotent_replay: bool


class EnqueueResearchExperiment:
    def __init__(self, repository: ResearchOperationsRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        queue_id: str,
        proposal_id: str,
        run_id: str,
        actor: str,
        idempotency_key: str,
        enqueued_at: datetime,
    ) -> EnqueuedResearchExperiment:
        retained = self._repository.queue_item(queue_id)
        if retained is not None:
            events = self._repository.queue_events(queue_id)
            if (
                retained.proposal_id != proposal_id
                or retained.run_id != run_id
                or retained.idempotency_key != idempotency_key
                or not events
            ):
                raise ResearchOperationsError("RESEARCH_QUEUE_IDEMPOTENCY_CONFLICT")
            return EnqueuedResearchExperiment(retained, events[0], True)
        proposal = self._repository.proposal(proposal_id)
        approval = self._repository.experiment_approval(proposal_id)
        feasibility = self._repository.proposal_feasibility(proposal_id)
        if proposal is None:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_NOT_FOUND")
        if approval is None or proposal_state(self._repository, proposal_id) is not (
            ResearchProposalState.APPROVED_FOR_EXPERIMENT
        ):
            raise ResearchOperationsError("RESEARCH_QUEUE_EXACT_APPROVAL_REQUIRED")
        if feasibility is None or feasibility.status is not (
            ResearchFeasibilityState.READY_FOR_REVIEW
        ):
            raise ResearchOperationsError("RESEARCH_QUEUE_FEASIBILITY_NOT_READY")
        active = tuple(
            value
            for value in self._repository.queue_items()
            if experiment_queue_status(self._repository, value.queue_id)
            in {ResearchExperimentQueueStatus.QUEUED, ResearchExperimentQueueStatus.RUNNING}
        )
        if len(active) >= MAXIMUM_QUEUED_EXPERIMENTS:
            raise ResearchOperationsError("RESEARCH_QUEUE_CAPACITY_EXCEEDED")
        experiment_id = approval.approved_experiment_id or dict(
            proposal.experiment_configuration
        ).get("existing_experiment_id")
        if not experiment_id:
            raise ResearchOperationsError("RESEARCH_QUEUE_EXACT_EXPERIMENT_ID_REQUIRED")
        item = ResearchExperimentQueueItem.create(
            queue_id=queue_id,
            proposal=proposal,
            approval=approval,
            exact_experiment_id=experiment_id,
            run_id=run_id,
            execution_order=len(active) + 1,
            idempotency_key=idempotency_key,
            actor=actor,
            enqueued_at=enqueued_at,
        )
        event = ResearchExperimentQueueEvent.create(
            event_id=f"{queue_id}-event-1",
            queue_id=queue_id,
            ordinal=1,
            from_status=None,
            to_status=ResearchExperimentQueueStatus.QUEUED,
            attempt_count=0,
            budget_event_id=None,
            result_id=None,
            recommendation_id=None,
            reason="EXACT_APPROVED_EXPERIMENT_QUEUED",
            actor=actor,
            idempotency_key=idempotency_key,
            occurred_at=enqueued_at,
        )
        self._repository.save_queue_item(item)
        self._repository.save_queue_event(event)
        return EnqueuedResearchExperiment(item, event, False)


class CancelQueuedResearchExperiment:
    def __init__(self, repository: ResearchOperationsRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        queue_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
        canceled_at: datetime,
    ) -> tuple[ResearchExperimentQueueEvent, bool]:
        item = self._repository.queue_item(queue_id)
        if item is None:
            raise ResearchOperationsError("RESEARCH_QUEUE_ITEM_NOT_FOUND")
        events = self._repository.queue_events(queue_id)
        retained = next(
            (
                value
                for value in events
                if value.to_status is ResearchExperimentQueueStatus.CANCELED
            ),
            None,
        )
        if retained is not None:
            if retained.idempotency_key != idempotency_key:
                raise ResearchOperationsError("RESEARCH_QUEUE_CANCEL_IDEMPOTENCY_CONFLICT")
            return retained, True
        if experiment_queue_status(self._repository, queue_id) is not (
            ResearchExperimentQueueStatus.QUEUED
        ):
            raise ResearchOperationsError("RESEARCH_QUEUE_CANCEL_STATE_INVALID")
        event = ResearchExperimentQueueEvent.create(
            event_id=f"{queue_id}-event-{len(events) + 1}",
            queue_id=queue_id,
            ordinal=len(events) + 1,
            from_status=ResearchExperimentQueueStatus.QUEUED,
            to_status=ResearchExperimentQueueStatus.CANCELED,
            attempt_count=0,
            budget_event_id=None,
            result_id=None,
            recommendation_id=None,
            reason=reason,
            actor=actor,
            idempotency_key=idempotency_key,
            occurred_at=canceled_at,
        )
        self._repository.save_queue_event(event)
        return event, False


@dataclass(frozen=True, slots=True)
class QueueExecutionResult:
    item: ResearchExperimentQueueItem
    event: ResearchExperimentQueueEvent
    governed_result: GovernedExperimentResult
    idempotent_replay: bool


class ExecuteApprovedResearchQueueItem:
    def __init__(
        self,
        repository: ResearchOperationsRepository,
        executor: ResearchExperimentExecutor,
        knowledge_source: ResearchKnowledgeSource | None = None,
    ) -> None:
        self._repository = repository
        self._runner = RunGovernedResearchExperiment(repository, executor, knowledge_source)

    def execute(
        self,
        *,
        queue_id: str,
        actor: str,
        idempotency_key: str,
        executed_at: datetime,
    ) -> QueueExecutionResult:
        item = self._repository.queue_item(queue_id)
        if item is None:
            raise ResearchOperationsError("RESEARCH_QUEUE_ITEM_NOT_FOUND")
        proposal = self._repository.proposal(item.proposal_id)
        if proposal is None or proposal.canonical_hash != item.proposal_hash:
            raise ResearchOperationsError("RESEARCH_QUEUE_PROPOSAL_BINDING_INVALID")
        status = experiment_queue_status(self._repository, queue_id)
        if status is ResearchExperimentQueueStatus.COMPLETED:
            events = self._repository.queue_events(queue_id)
            completed = events[-1]
            result = self._runner.execute(
                proposal_id=proposal.proposal_id,
                run_id=item.run_id,
                actor=actor,
                confirmation=f"RUN:{proposal.proposal_id}:{item.run_id}:{proposal.canonical_hash}",
                executed_at=executed_at,
            )
            return QueueExecutionResult(item, completed, result, True)
        if status is not ResearchExperimentQueueStatus.QUEUED:
            raise ResearchOperationsError("RESEARCH_QUEUE_EXECUTION_STATE_INVALID")
        if any(
            experiment_queue_status(self._repository, value.queue_id)
            is ResearchExperimentQueueStatus.RUNNING
            for value in self._repository.queue_items()
            if value.queue_id != queue_id
        ):
            raise ResearchOperationsError("RESEARCH_QUEUE_CONCURRENCY_LIMIT")
        events = self._repository.queue_events(queue_id)
        running = ResearchExperimentQueueEvent.create(
            event_id=f"{queue_id}-event-{len(events) + 1}",
            queue_id=queue_id,
            ordinal=len(events) + 1,
            from_status=ResearchExperimentQueueStatus.QUEUED,
            to_status=ResearchExperimentQueueStatus.RUNNING,
            attempt_count=1,
            budget_event_id=None,
            result_id=None,
            recommendation_id=None,
            reason="EXACT_APPROVED_QUEUE_ITEM_STARTED",
            actor=actor,
            idempotency_key=idempotency_key,
            occurred_at=executed_at,
        )
        self._repository.save_queue_event(running)
        try:
            result = self._runner.execute(
                proposal_id=proposal.proposal_id,
                run_id=item.run_id,
                actor=actor,
                confirmation=f"RUN:{proposal.proposal_id}:{item.run_id}:{proposal.canonical_hash}",
                executed_at=executed_at,
            )
        except Exception as error:
            failed = ResearchExperimentQueueEvent.create(
                event_id=f"{queue_id}-event-{len(events) + 2}",
                queue_id=queue_id,
                ordinal=len(events) + 2,
                from_status=ResearchExperimentQueueStatus.RUNNING,
                to_status=ResearchExperimentQueueStatus.FAILED,
                attempt_count=1,
                budget_event_id=None,
                result_id=None,
                recommendation_id=None,
                reason=f"EXPERIMENT_FAILED:{type(error).__name__}",
                actor=actor,
                idempotency_key=idempotency_key,
                occurred_at=executed_at,
            )
            self._repository.save_queue_event(failed)
            raise
        budget_event = next(
            (
                value
                for value in reversed(self._repository.budget_events(proposal.proposal_id))
                if value.run_id == item.run_id
                and value.event_type
                in {ResearchBudgetEventType.EXPERIMENT_RUN, ResearchBudgetEventType.FAILED_RUN}
            ),
            None,
        )
        completed = ResearchExperimentQueueEvent.create(
            event_id=f"{queue_id}-event-{len(events) + 2}",
            queue_id=queue_id,
            ordinal=len(events) + 2,
            from_status=ResearchExperimentQueueStatus.RUNNING,
            to_status=ResearchExperimentQueueStatus.COMPLETED,
            attempt_count=1,
            budget_event_id=None if budget_event is None else budget_event.event_id,
            result_id=result.outcome.result_hash,
            recommendation_id=result.recommendation.recommendation_id,
            reason=f"RECOMMENDATION_{result.recommendation.state.value}_RETAINED",
            actor=actor,
            idempotency_key=idempotency_key,
            occurred_at=executed_at,
        )
        self._repository.save_queue_event(completed)
        return QueueExecutionResult(item, completed, result, False)


def research_review_queue(
    repository: ResearchOperationsRepository,
    knowledge_source: ResearchKnowledgeSource | None = None,
) -> tuple[dict[str, object], ...]:
    knowledge = knowledge_source or EmptyResearchKnowledgeSource()
    values: list[dict[str, object]] = []
    for priority in repository.proposal_priorities():
        proposal = repository.proposal(priority.proposal_id)
        agenda = repository.agenda_item(priority.agenda_item_id)
        feasibility = repository.proposal_feasibility(priority.proposal_id)
        if proposal is None or agenda is None or feasibility is None:
            raise ResearchOperationsError("RESEARCH_REVIEW_QUEUE_LINEAGE_INCOMPLETE")
        duplicate = duplicate_decision(repository, proposal, knowledge)
        values.append(
            {
                "proposal": proposal,
                "agenda_item": agenda,
                "proposal_class": priority.proposal_class,
                "priority": priority,
                "duplicate_decision": duplicate,
                "feasibility": feasibility,
                "budget": summarize_research_budget(
                    proposal, repository.budget_events(proposal.proposal_id)
                ),
                "review_status": proposal_state(repository, proposal.proposal_id),
                "estimated_public_requests": (
                    1
                    if any(value.startswith("MISSING:") for value in proposal.required_data_ids)
                    else 0
                ),
                "estimated_runtime_seconds": 60,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            }
        )
    return tuple(values)


def research_operations_workspace(
    repository: ResearchOperationsRepository,
    knowledge_source: ResearchKnowledgeSource | None = None,
) -> dict[str, object]:
    knowledge = knowledge_source or EmptyResearchKnowledgeSource()
    proposals = tuple(
        {
            "proposal": proposal,
            "state": proposal_state(repository, proposal.proposal_id),
            "duplicate_decision": duplicate_decision(repository, proposal, knowledge),
            "reviews": repository.reviews(proposal.proposal_id),
            "budget": summarize_research_budget(
                proposal, repository.budget_events(proposal.proposal_id)
            ),
            "recommendations": tuple(
                value
                for value in repository.recommendations()
                if value.proposal_id == proposal.proposal_id
            ),
        }
        for proposal in repository.proposals()
    )
    queue = tuple(
        {
            "item": item,
            "events": repository.queue_events(item.queue_id),
            "status": experiment_queue_status(repository, item.queue_id),
            "attempt_count": max(
                (value.attempt_count for value in repository.queue_events(item.queue_id)),
                default=0,
            ),
        }
        for item in repository.queue_items()
    )
    recommendations = repository.recommendations()
    agenda_projection = tuple(
        {
            **asdict(item),
            "retained_status": item.status,
            "status": (
                ResearchAgendaStatus.RESOLVED
                if any(
                    proposal.agenda_item_id == item.agenda_item_id
                    and proposal_state(repository, proposal.proposal_id)
                    is ResearchProposalState.COMPLETED
                    for proposal in repository.proposals()
                )
                else ResearchAgendaStatus.DEFERRED
                if any(
                    proposal.agenda_item_id == item.agenda_item_id
                    and proposal_state(repository, proposal.proposal_id)
                    in {ResearchProposalState.REJECTED, ResearchProposalState.DEFERRED}
                    for proposal in repository.proposals()
                )
                else ResearchAgendaStatus.PROPOSED
                if any(
                    proposal.agenda_item_id == item.agenda_item_id
                    for proposal in repository.proposals()
                )
                else item.status
            ),
        }
        for item in repository.agenda_items()
    )
    return {
        "agenda_items": agenda_projection,
        "proposals": proposals,
        "generator_runs": repository.generator_runs(),
        "proposal_priorities": repository.proposal_priorities(),
        "proposal_feasibilities": repository.proposal_feasibilities(),
        "review_queue": research_review_queue(repository, knowledge),
        "experiment_queue": queue,
        "recommendations": recommendations,
        "first_generated_method_selection": repository.first_method_selection(),
        "first_generated_method_implementation": repository.first_method_implementation(),
        "first_generated_method_runs": repository.first_method_runs(),
        "follow_up_attention": tuple(
            {
                "recommendation_id": value.recommendation_id,
                "proposal_id": value.proposal_id,
                "state": value.state,
                "reason": f"RESEARCH_RESULT_REQUIRES_{value.state.value}_ATTENTION",
                "authority": "NONE",
            }
            for value in recommendations
        ),
        "authority": "NONE",
        "paper_authority": "NONE",
        "live_authority": "NONE",
        "external_ai_model_calls_enabled": False,
        "automatic_approval": False,
        "automatic_experiment_execution": False,
    }
