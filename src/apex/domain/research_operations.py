"""Pure rules for governed, authority-free research operations.

This module classifies retained facts and research proposals.  It cannot run
code, contact a provider, create financial state, or grant PAPER/LIVE authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import content_digest

RESEARCH_AGENDA_POLICY_V1 = "RESEARCH_AGENDA_POLICY_V1"
RESEARCH_DUPLICATION_POLICY_V1 = "RESEARCH_DUPLICATION_POLICY_V1"
RESEARCH_BUDGET_POLICY_V1 = "RESEARCH_BUDGET_POLICY_V1"
RESEARCH_RECOMMENDATION_POLICY_V1 = "RESEARCH_RECOMMENDATION_POLICY_V1"
GOVERNED_RESEARCH_PROPOSAL_V1 = "GOVERNED_RESEARCH_PROPOSAL_V1"
RESEARCH_PROPOSAL_GENERATOR_V1 = "RESEARCH_PROPOSAL_GENERATOR_V1"
RESEARCH_PROPOSAL_GENERATION_POLICY_V1 = "RESEARCH_PROPOSAL_GENERATION_POLICY_V1"
RESEARCH_PROPOSAL_FEASIBILITY_POLICY_V1 = "RESEARCH_PROPOSAL_FEASIBILITY_POLICY_V1"
RESEARCH_EXPERIMENT_QUEUE_POLICY_V1 = "RESEARCH_EXPERIMENT_QUEUE_POLICY_V1"
FIRST_GENERATED_RESEARCH_METHOD_SELECTION_V1 = "FIRST_GENERATED_RESEARCH_METHOD_SELECTION_V1"
PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION_V1 = (
    "PAPER_READINESS_EXISTING_METHOD_OPERATIONALIZATION_V1"
)
FIRST_GENERATED_RESEARCH_METHOD_IMPLEMENTATION_V1 = (
    "FIRST_GENERATED_RESEARCH_METHOD_IMPLEMENTATION_V1"
)

MAXIMUM_GENERATED_PROPOSALS = 3
MAXIMUM_QUEUED_EXPERIMENTS = 3


class ResearchOperationsError(ValueError):
    """A governed research operation failed closed."""


class ResearchVenue(StrEnum):
    CRYPTO = "CRYPTO"
    KALSHI = "KALSHI"
    CROSS_VENUE = "CROSS_VENUE"
    # Securities Structure Forward Performance v1: the first venue-specific
    # member added since this enum's original three. Additive only -- every
    # existing CRYPTO/KALSHI/CROSS_VENUE fact/proposal/persisted row is
    # untouched; see ``domain.securities_structure_outcome`` for the
    # evidence this feeds.
    SECURITIES = "SECURITIES"


class ResearchAgendaStatus(StrEnum):
    OPEN = "OPEN"
    PROPOSED = "PROPOSED"
    DEFERRED = "DEFERRED"
    RESOLVED = "RESOLVED"


class ResearchProposalOrigin(StrEnum):
    OPERATOR = "OPERATOR"
    SYSTEM_HEURISTIC = "SYSTEM_HEURISTIC"
    AI_ASSISTED = "AI_ASSISTED"
    IMPORTED = "IMPORTED"


class ResearchProposalState(StrEnum):
    DRAFT = "DRAFT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED_FOR_EXPERIMENT = "APPROVED_FOR_EXPERIMENT"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    DUPLICATE = "DUPLICATE"
    INVALID = "INVALID"


class ResearchProposalClass(StrEnum):
    EVIDENCE_COMPLETION = "EVIDENCE_COMPLETION"
    REPLICATION = "REPLICATION"
    MATERIAL_CHALLENGER = "MATERIAL_CHALLENGER"
    OPERATIONAL_METHOD = "OPERATIONAL_METHOD"


class ResearchFeasibilityState(StrEnum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    DUPLICATE = "DUPLICATE"
    REQUIRES_EVIDENCE = "REQUIRES_EVIDENCE"
    REQUIRES_IMPLEMENTATION = "REQUIRES_IMPLEMENTATION"
    BUDGET_BLOCKED = "BUDGET_BLOCKED"
    INVALID = "INVALID"


class ResearchExperimentQueueStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELED = "CANCELED"


class DuplicateClassification(StrEnum):
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    METADATA_ONLY_CHANGE = "METADATA_ONLY_CHANGE"
    PARAMETER_ONLY_VARIATION = "PARAMETER_ONLY_VARIATION"
    VALID_REPLICATION = "VALID_REPLICATION"
    MATERIALLY_DIFFERENT_CHALLENGER = "MATERIALLY_DIFFERENT_CHALLENGER"
    GENUINELY_NEW_METHOD_FAMILY = "GENUINELY_NEW_METHOD_FAMILY"


class ResearchBudgetEventType(StrEnum):
    APPROVAL_RESERVED = "APPROVAL_RESERVED"
    EXPERIMENT_RUN = "EXPERIMENT_RUN"
    REPLAY = "REPLAY"
    FAILED_RUN = "FAILED_RUN"
    SUPERSEDED_DEFECT_RUN = "SUPERSEDED_DEFECT_RUN"


class ResearchRecommendationState(StrEnum):
    REJECT = "REJECT"
    REVISE = "REVISE"
    REPLICATE = "REPLICATE"
    PAPER_CANDIDATE = "PAPER_CANDIDATE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS = 2000


def generated_proposal_hypothesis_prefix(proposal_class: ResearchProposalClass) -> str:
    return f"Bounded {proposal_class.value.lower()} work may resolve "


def generated_proposal_hypothesis(
    proposal_class: ResearchProposalClass, problem_statement: str
) -> str:
    """The generator's hypothesis text: a class prefix plus the agenda item's
    problem statement, which is the source fact's summary verbatim."""

    return f"{generated_proposal_hypothesis_prefix(proposal_class)}{problem_statement}"


MAXIMUM_HYPOTHESIS_PREFIX_CHARS = max(
    len(generated_proposal_hypothesis_prefix(value)) for value in ResearchProposalClass
)
# A fact summary becomes the agenda item's problem statement and is embedded
# verbatim in a generated proposal hypothesis, so the summary bound is derived
# from the hypothesis ceiling rather than chosen independently of it.
RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS = (
    RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS - MAXIMUM_HYPOTHESIS_PREFIX_CHARS
)


def _text(value: str, reason: str, maximum: int = 500) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ResearchOperationsError(reason)
    return normalized


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise ResearchOperationsError(reason)
    return value.astimezone(UTC)


def _pairs(values: tuple[tuple[str, str], ...], reason: str) -> tuple[tuple[str, str], ...]:
    normalized = tuple(
        sorted((_text(key, reason, 160), _text(value, reason, 1000)) for key, value in values)
    )
    if len({key for key, _ in normalized}) != len(normalized):
        raise ResearchOperationsError(reason)
    return normalized


@dataclass(frozen=True, slots=True)
class ResearchAgendaSourceFact:
    fact_id: str
    fact_type: str
    venue: ResearchVenue
    market_scope: tuple[str, ...]
    state: str
    summary: str
    evidence_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    observed_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        fact_id: str,
        fact_type: str,
        venue: ResearchVenue,
        market_scope: tuple[str, ...],
        state: str,
        summary: str,
        evidence_ids: tuple[str, ...],
        evidence_hashes: tuple[str, ...],
        observed_at: datetime,
    ) -> ResearchAgendaSourceFact:
        if not evidence_ids or len(evidence_ids) != len(evidence_hashes):
            raise ResearchOperationsError("RESEARCH_AGENDA_LINEAGE_INVALID")
        hashes = tuple(
            sorted(_text(value, "RESEARCH_AGENDA_HASH_INVALID", 64) for value in evidence_hashes)
        )
        if any(len(value) != 64 for value in hashes):
            raise ResearchOperationsError("RESEARCH_AGENDA_HASH_INVALID")
        values = (
            _text(fact_id, "RESEARCH_AGENDA_FACT_ID_INVALID", 300),
            _text(fact_type, "RESEARCH_AGENDA_FACT_TYPE_INVALID", 100),
            venue,
            tuple(
                sorted(_text(value, "RESEARCH_AGENDA_SCOPE_INVALID", 300) for value in market_scope)
            ),
            _text(state, "RESEARCH_AGENDA_FACT_STATE_INVALID", 100),
            _text(
                summary,
                "RESEARCH_AGENDA_FACT_SUMMARY_INVALID",
                RESEARCH_AGENDA_FACT_SUMMARY_MAX_CHARS,
            ),
            tuple(
                sorted(
                    _text(value, "RESEARCH_AGENDA_EVIDENCE_ID_INVALID", 300)
                    for value in evidence_ids
                )
            ),
            hashes,
            _time(observed_at, "RESEARCH_AGENDA_FACT_TIME_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest((RESEARCH_AGENDA_POLICY_V1, *values)), "NONE")


@dataclass(frozen=True, slots=True)
class ResearchAgendaItem:
    agenda_item_id: str
    source_fact_id: str
    source_fact_hash: str
    venue: ResearchVenue
    market_scope: tuple[str, ...]
    problem_statement: str
    why_it_matters: str
    prior_related_research: tuple[str, ...]
    duplicate_risk_status: str
    suggested_next_step: str
    priority: int
    status: ResearchAgendaStatus
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"


def build_research_agenda_item(
    fact: ResearchAgendaSourceFact,
    *,
    prior_related_research: tuple[str, ...] = (),
    priority: int = 50,
) -> ResearchAgendaItem:
    if not 1 <= priority <= 100:
        raise ResearchOperationsError("RESEARCH_AGENDA_PRIORITY_INVALID")
    next_steps = {
        "INSUFFICIENT_CALIBRATION": "COLLECT_OR_REPLICATE_COMPATIBLE_COMPLETED_OUTCOMES",
        "NEGATIVE_EXPECTED_ECONOMICS": "REVISE_MECHANISM_OR_RETAIN_NEGATIVE_KNOWLEDGE",
        "NON_POSITIVE_NET_GAP": "WAIT_FOR_NEW_PUBLIC_EVIDENCE_OR_REVISE_SCOPE",
        "EXHAUSTIVENESS_UNPROVEN": "RETAIN_EXACT_RULE_EVIDENCE_BEFORE_REEVALUATION",
        "ZERO_SHARED_ELIGIBLE": "INSPECT_SOURCE_BLOCKERS_WITHOUT_LOOSENING_POLICY",
        "ZERO_PAPER_READY": "RETAIN_MORE_SOURCE_AND_LIQUIDITY_EVIDENCE",
        "FAILED_STRATEGY_HEALTH": "REVIEW_FAILED_THRESHOLDS_AND_REPLICATION_NEED",
        "DATA_QUALITY_LIMITATION": "REPAIR_OR_EXTEND_RETAINED_EVIDENCE",
        "COUNTERFACTUAL_OUTCOME_PATTERN": (
            "REVIEW_COUNTERFACTUAL_OUTCOME_EVIDENCE_FOR_POLICY_IMPLICATIONS"
        ),
        "SECURITIES_STRUCTURE_OUTCOME_PATTERN": (
            "REVIEW_SECURITIES_STRUCTURE_OUTCOME_FOR_COMPARATIVE_RESEARCH_CONCLUSIONS"
        ),
    }
    duplicate_risk = "REQUIRES_NEGATIVE_KNOWLEDGE_CHECK"
    suggested = next_steps.get(fact.fact_type, "REVIEW_RETAINED_EVIDENCE_AND_DEFINE_BOUNDED_TEST")
    values = (
        f"agenda-{fact.fact_id}",
        fact.fact_id,
        fact.canonical_hash,
        fact.venue,
        fact.market_scope,
        fact.summary,
        f"Retained {fact.fact_type} evidence currently blocks or weakens a research conclusion.",
        tuple(sorted(set(prior_related_research))),
        duplicate_risk,
        suggested,
        priority,
        ResearchAgendaStatus.OPEN,
        fact.observed_at,
        "NONE",
    )
    return ResearchAgendaItem(
        *values[:-1], content_digest((RESEARCH_AGENDA_POLICY_V1, *values)), "NONE"
    )


_SUPPORTED_DEFINITION_KINDS = frozenset(
    {
        "CRYPTO_GENERIC_LONG_CASH",
        "CRYPTO_CURRENT_METHOD",
        "KALSHI_RETAINED_RELATIONSHIP_METHOD",
        "EXISTING_RESEARCH_REPLICATION",
        "REQUIRES_IMPLEMENTATION",
    }
)


@dataclass(frozen=True, slots=True)
class ResearchProposal:
    proposal_id: str
    version: int
    agenda_item_id: str
    venue: ResearchVenue
    hypothesis: str
    expected_mechanism: str
    instruments: tuple[str, ...]
    proposed_family: str
    definition_kind: str
    strategy_definition: tuple[tuple[str, str], ...]
    exact_change_from_prior: str
    prior_research_ids: tuple[str, ...]
    required_data_ids: tuple[str, ...]
    experiment_configuration: tuple[tuple[str, str], ...]
    validation_windows: tuple[tuple[str, str], ...]
    cost_assumptions: tuple[tuple[str, str], ...]
    success_criteria: tuple[str, ...]
    failure_criteria: tuple[str, ...]
    maximum_experiment_runs: int
    origin: ResearchProposalOrigin
    actor: str
    warnings: tuple[str, ...]
    created_at: datetime
    semantic_hash: str
    experiment_configuration_hash: str
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        version: int,
        agenda_item_id: str,
        venue: ResearchVenue,
        hypothesis: str,
        expected_mechanism: str,
        instruments: tuple[str, ...],
        proposed_family: str,
        definition_kind: str,
        strategy_definition: tuple[tuple[str, str], ...],
        exact_change_from_prior: str,
        prior_research_ids: tuple[str, ...],
        required_data_ids: tuple[str, ...],
        experiment_configuration: tuple[tuple[str, str], ...],
        validation_windows: tuple[tuple[str, str], ...],
        cost_assumptions: tuple[tuple[str, str], ...],
        success_criteria: tuple[str, ...],
        failure_criteria: tuple[str, ...],
        maximum_experiment_runs: int,
        origin: ResearchProposalOrigin,
        actor: str,
        warnings: tuple[str, ...],
        created_at: datetime,
    ) -> ResearchProposal:
        if version <= 0 or not 1 <= maximum_experiment_runs <= 12:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_BUDGET_INVALID")
        if definition_kind not in _SUPPORTED_DEFINITION_KINDS:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_DEFINITION_KIND_UNSUPPORTED")
        if not instruments or not required_data_ids or not success_criteria or not failure_criteria:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_EVIDENCE_OR_CRITERIA_MISSING")
        definition = _pairs(strategy_definition, "RESEARCH_PROPOSAL_DEFINITION_INVALID")
        configuration = _pairs(experiment_configuration, "RESEARCH_PROPOSAL_CONFIGURATION_INVALID")
        windows = _pairs(validation_windows, "RESEARCH_PROPOSAL_WINDOW_INVALID")
        costs = _pairs(cost_assumptions, "RESEARCH_PROPOSAL_COST_INVALID")
        normalized_instruments = tuple(
            sorted(
                set(
                    _text(value, "RESEARCH_PROPOSAL_INSTRUMENT_INVALID", 300)
                    for value in instruments
                )
            )
        )
        family = _text(proposed_family, "RESEARCH_PROPOSAL_FAMILY_INVALID", 300)
        mechanism = _text(expected_mechanism, "RESEARCH_PROPOSAL_MECHANISM_INVALID", 2000)
        semantic_hash = content_digest(
            (venue, family, definition_kind, normalized_instruments, mechanism, definition)
        )
        configuration_hash = content_digest((configuration, windows, costs))
        base = (
            _text(proposal_id, "RESEARCH_PROPOSAL_ID_INVALID", 300),
            version,
            _text(agenda_item_id, "RESEARCH_PROPOSAL_AGENDA_ID_INVALID", 300),
            venue,
            _text(
                hypothesis,
                "RESEARCH_PROPOSAL_HYPOTHESIS_INVALID",
                RESEARCH_PROPOSAL_HYPOTHESIS_MAX_CHARS,
            ),
            mechanism,
            normalized_instruments,
            family,
            definition_kind,
            definition,
            _text(exact_change_from_prior, "RESEARCH_PROPOSAL_CHANGE_INVALID", 2000),
            tuple(sorted(set(prior_research_ids))),
            tuple(sorted(set(required_data_ids))),
            configuration,
            windows,
            costs,
            tuple(sorted(set(success_criteria))),
            tuple(sorted(set(failure_criteria))),
            maximum_experiment_runs,
            origin,
            _text(actor, "RESEARCH_PROPOSAL_ACTOR_INVALID", 300),
            tuple(sorted(set(warnings))),
            _time(created_at, "RESEARCH_PROPOSAL_TIME_INVALID"),
            semantic_hash,
            configuration_hash,
            "NONE",
            False,
            False,
        )
        return cls(*base[:-3], content_digest((GOVERNED_RESEARCH_PROPOSAL_V1, *base)), *base[-3:])


@dataclass(frozen=True, slots=True)
class ResearchProposalGeneratorRun:
    generator_run_id: str
    generator_id: str
    policy_id: str
    agenda_item_ids: tuple[str, ...]
    agenda_item_hashes: tuple[str, ...]
    generated_proposal_ids: tuple[str, ...]
    maximum_proposals: int
    origin: ResearchProposalOrigin
    actor: str
    external_model_called: bool
    generated_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        generator_run_id: str,
        agenda_items: tuple[ResearchAgendaItem, ...],
        generated_proposal_ids: tuple[str, ...],
        maximum_proposals: int,
        origin: ResearchProposalOrigin,
        actor: str,
        external_model_called: bool,
        generated_at: datetime,
    ) -> ResearchProposalGeneratorRun:
        if not 1 <= maximum_proposals <= MAXIMUM_GENERATED_PROPOSALS:
            raise ResearchOperationsError("RESEARCH_GENERATOR_LIMIT_INVALID")
        if len(generated_proposal_ids) > maximum_proposals:
            raise ResearchOperationsError("RESEARCH_GENERATOR_OUTPUT_LIMIT_EXCEEDED")
        if external_model_called:
            raise ResearchOperationsError("RESEARCH_GENERATOR_EXTERNAL_MODEL_DISABLED")
        agenda_ids = tuple(value.agenda_item_id for value in agenda_items)
        agenda_hashes = tuple(value.canonical_hash for value in agenda_items)
        if len(set(agenda_ids)) != len(agenda_ids):
            raise ResearchOperationsError("RESEARCH_GENERATOR_AGENDA_DUPLICATE")
        values = (
            _text(generator_run_id, "RESEARCH_GENERATOR_RUN_ID_INVALID", 300),
            RESEARCH_PROPOSAL_GENERATOR_V1,
            RESEARCH_PROPOSAL_GENERATION_POLICY_V1,
            agenda_ids,
            agenda_hashes,
            tuple(generated_proposal_ids),
            maximum_proposals,
            origin,
            _text(actor, "RESEARCH_GENERATOR_ACTOR_INVALID", 300),
            False,
            _time(generated_at, "RESEARCH_GENERATOR_TIME_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest(values), "NONE")


@dataclass(frozen=True, slots=True)
class ResearchProposalPriority:
    priority_id: str
    generator_run_id: str
    proposal_id: str
    agenda_item_id: str
    proposal_class: ResearchProposalClass
    importance_score: int
    evidence_score: int
    novelty_score: int
    execution_cost_score: int
    runtime_score: int
    decision_value_score: int
    library_coverage_score: int
    total_score: int
    deterministic_rank: int
    tie_break_key: str
    policy_id: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        generator_run_id: str,
        proposal: ResearchProposal,
        proposal_class: ResearchProposalClass,
        importance_score: int,
        evidence_score: int,
        novelty_score: int,
        execution_cost_score: int,
        runtime_score: int,
        decision_value_score: int,
        library_coverage_score: int,
        deterministic_rank: int,
    ) -> ResearchProposalPriority:
        components = (
            importance_score,
            evidence_score,
            novelty_score,
            execution_cost_score,
            runtime_score,
            decision_value_score,
            library_coverage_score,
        )
        if any(not 0 <= value <= 100 for value in components):
            raise ResearchOperationsError("RESEARCH_PROPOSAL_PRIORITY_COMPONENT_INVALID")
        if not 1 <= deterministic_rank <= MAXIMUM_GENERATED_PROPOSALS:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_PRIORITY_RANK_INVALID")
        total = (
            importance_score * 30
            + evidence_score * 20
            + novelty_score * 15
            + execution_cost_score * 10
            + runtime_score * 5
            + decision_value_score * 15
            + library_coverage_score * 5
        ) // 100
        tie_break = f"{proposal.agenda_item_id}:{proposal.proposed_family}:{proposal.proposal_id}"
        values = (
            f"priority-{proposal.proposal_id}",
            generator_run_id,
            proposal.proposal_id,
            proposal.agenda_item_id,
            proposal_class,
            *components,
            total,
            deterministic_rank,
            tie_break,
            RESEARCH_PROPOSAL_GENERATION_POLICY_V1,
            "NONE",
        )
        return cls(*values[:-1], content_digest(values), "NONE")


@dataclass(frozen=True, slots=True)
class ResearchProposalFeasibility:
    feasibility_id: str
    generator_run_id: str
    proposal_id: str
    proposal_hash: str
    duplicate_classification: DuplicateClassification
    duplicate_decision_hash: str
    schema_valid: bool
    negative_knowledge_passed: bool
    data_available: bool
    provider_capability_available: bool
    experiment_engine_compatible: bool
    budget_available: bool
    evidence_class_compatible: bool
    authority_restrictions_passed: bool
    status: ResearchFeasibilityState
    reasons: tuple[str, ...]
    policy_id: str
    evaluated_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    implementation_id: str | None = None
    supersedes_feasibility_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        generator_run_id: str,
        proposal: ResearchProposal,
        duplicate_decision: DuplicateDecision,
        schema_valid: bool,
        data_available: bool,
        provider_capability_available: bool,
        experiment_engine_compatible: bool,
        budget_available: bool,
        evidence_class_compatible: bool,
        authority_restrictions_passed: bool,
        evaluated_at: datetime,
        feasibility_id: str | None = None,
        implementation_id: str | None = None,
        supersedes_feasibility_id: str | None = None,
    ) -> ResearchProposalFeasibility:
        negative_passed = duplicate_decision.approval_permitted
        reasons: list[str] = []
        if not schema_valid:
            reasons.append("PROPOSAL_SCHEMA_INVALID")
        if not negative_passed:
            reasons.extend(duplicate_decision.reasons)
        if not data_available:
            reasons.append("REQUIRED_RETAINED_DATA_UNAVAILABLE")
        if not provider_capability_available:
            reasons.append("BOUNDED_PROVIDER_CAPABILITY_OR_CAPTURE_REQUIRED")
        if not experiment_engine_compatible:
            reasons.append("EXPERIMENT_ENGINE_OR_PRIMITIVE_UNSUPPORTED")
        if not budget_available:
            reasons.append("EXPERIMENT_BUDGET_UNAVAILABLE")
        if not evidence_class_compatible:
            reasons.append("EVIDENCE_CLASS_INCOMPATIBLE")
        if not authority_restrictions_passed:
            reasons.append("AUTHORITY_RESTRICTION_FAILED")
        if not schema_valid or not evidence_class_compatible or not authority_restrictions_passed:
            status = ResearchFeasibilityState.INVALID
        elif not negative_passed:
            status = ResearchFeasibilityState.DUPLICATE
        elif not experiment_engine_compatible or (
            proposal.definition_kind == "REQUIRES_IMPLEMENTATION" and implementation_id is None
        ):
            status = ResearchFeasibilityState.REQUIRES_IMPLEMENTATION
        elif not data_available or not provider_capability_available:
            status = ResearchFeasibilityState.REQUIRES_EVIDENCE
        elif not budget_available:
            status = ResearchFeasibilityState.BUDGET_BLOCKED
        else:
            status = ResearchFeasibilityState.READY_FOR_REVIEW
            reasons.append("ALL_BOUNDED_FEASIBILITY_GATES_PASSED")
        identity = feasibility_id or f"feasibility-{proposal.proposal_id}"
        values = (
            _text(identity, "RESEARCH_FEASIBILITY_ID_INVALID", 300),
            generator_run_id,
            proposal.proposal_id,
            proposal.canonical_hash,
            duplicate_decision.classification,
            duplicate_decision.canonical_hash,
            schema_valid,
            negative_passed,
            data_available,
            provider_capability_available,
            experiment_engine_compatible,
            budget_available,
            evidence_class_compatible,
            authority_restrictions_passed,
            status,
            tuple(reasons),
            RESEARCH_PROPOSAL_FEASIBILITY_POLICY_V1,
            _time(evaluated_at, "RESEARCH_FEASIBILITY_TIME_INVALID"),
            "NONE",
            implementation_id,
            supersedes_feasibility_id,
        )
        return cls(
            feasibility_id=values[0],
            generator_run_id=values[1],
            proposal_id=values[2],
            proposal_hash=values[3],
            duplicate_classification=values[4],
            duplicate_decision_hash=values[5],
            schema_valid=values[6],
            negative_knowledge_passed=values[7],
            data_available=values[8],
            provider_capability_available=values[9],
            experiment_engine_compatible=values[10],
            budget_available=values[11],
            evidence_class_compatible=values[12],
            authority_restrictions_passed=values[13],
            status=values[14],
            reasons=values[15],
            policy_id=values[16],
            evaluated_at=values[17],
            canonical_hash=content_digest(values),
            authority="NONE",
            implementation_id=implementation_id,
            supersedes_feasibility_id=supersedes_feasibility_id,
        )


@dataclass(frozen=True, slots=True)
class PriorResearchKnowledge:
    research_id: str
    venue: ResearchVenue
    proposed_family: str
    semantic_hash: str
    experiment_configuration_hash: str
    validation_windows: tuple[tuple[str, str], ...]
    rejected: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    proposal_id: str
    classification: DuplicateClassification
    related_research_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    approval_permitted: bool
    policy_id: str
    canonical_hash: str


def classify_research_proposal(
    proposal: ResearchProposal,
    prior: tuple[PriorResearchKnowledge, ...],
) -> DuplicateDecision:
    exact = tuple(
        item
        for item in prior
        if item.semantic_hash == proposal.semantic_hash
        and item.experiment_configuration_hash == proposal.experiment_configuration_hash
    )
    same_semantic = tuple(item for item in prior if item.semantic_hash == proposal.semantic_hash)
    same_family = tuple(
        item
        for item in prior
        if item.venue is proposal.venue and item.proposed_family == proposal.proposed_family
    )
    replication_requested = proposal.definition_kind == "EXISTING_RESEARCH_REPLICATION"
    disjoint = all(
        set(item.validation_windows).isdisjoint(proposal.validation_windows)
        for item in same_semantic
    )
    if exact:
        classification = DuplicateClassification.EXACT_DUPLICATE
        reasons = ("SEMANTIC_AND_EXPERIMENT_CONFIGURATION_ALREADY_RETAINED",)
        permitted = False
        related = tuple(item.research_id for item in exact)
    elif same_semantic and replication_requested and disjoint:
        classification = DuplicateClassification.VALID_REPLICATION
        reasons = ("EXACT_SEMANTICS_WITH_DECLARED_DISJOINT_VALIDATION",)
        permitted = True
        related = tuple(item.research_id for item in same_semantic)
    elif same_semantic and proposal.exact_change_from_prior.upper().startswith("METADATA"):
        classification = DuplicateClassification.METADATA_ONLY_CHANGE
        reasons = ("NO_MATERIAL_SEMANTIC_OR_EXPERIMENT_CHANGE",)
        permitted = False
        related = tuple(item.research_id for item in same_semantic)
    elif same_semantic:
        classification = DuplicateClassification.PARAMETER_ONLY_VARIATION
        rejected = any(item.rejected for item in same_semantic)
        reasons = (
            ("REJECTED_SEMANTICS_CANNOT_BE_REINTRODUCED_BY_PARAMETER_CHANGE",)
            if rejected
            else ("SAME_SEMANTICS_WITH_DIFFERENT_EXPERIMENT_CONFIGURATION",)
        )
        permitted = not rejected
        related = tuple(item.research_id for item in same_semantic)
    elif same_family:
        classification = DuplicateClassification.MATERIALLY_DIFFERENT_CHALLENGER
        reasons = ("SAME_FAMILY_WITH_DISTINCT_SEMANTIC_HASH",)
        permitted = True
        related = tuple(item.research_id for item in same_family)
    else:
        classification = DuplicateClassification.GENUINELY_NEW_METHOD_FAMILY
        reasons = ("NO_RETAINED_FAMILY_OR_SEMANTIC_MATCH",)
        permitted = True
        related = ()
    values = (
        proposal.proposal_id,
        classification,
        tuple(sorted(related)),
        reasons,
        permitted,
        RESEARCH_DUPLICATION_POLICY_V1,
    )
    return DuplicateDecision(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class ResearchProposalReview:
    review_id: str
    proposal_id: str
    ordinal: int
    from_state: ResearchProposalState
    to_state: ResearchProposalState
    duplicate_classification: DuplicateClassification
    duplicate_decision_hash: str
    actor: str
    reason: str
    confirmation: str
    reviewed_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        review_id: str,
        proposal_id: str,
        ordinal: int,
        from_state: ResearchProposalState,
        to_state: ResearchProposalState,
        duplicate_decision: DuplicateDecision,
        actor: str,
        reason: str,
        confirmation: str,
        reviewed_at: datetime,
    ) -> ResearchProposalReview:
        if ordinal <= 0:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_REVIEW_ORDINAL_INVALID")
        allowed = {
            ResearchProposalState.DRAFT: {
                ResearchProposalState.READY_FOR_REVIEW,
                ResearchProposalState.INVALID,
            },
            ResearchProposalState.READY_FOR_REVIEW: {
                ResearchProposalState.APPROVED_FOR_EXPERIMENT,
                ResearchProposalState.REJECTED,
                ResearchProposalState.DEFERRED,
                ResearchProposalState.DUPLICATE,
                ResearchProposalState.INVALID,
            },
            ResearchProposalState.APPROVED_FOR_EXPERIMENT: {
                ResearchProposalState.RUNNING,
                ResearchProposalState.REJECTED,
                ResearchProposalState.DEFERRED,
            },
            ResearchProposalState.RUNNING: {
                ResearchProposalState.COMPLETED,
                ResearchProposalState.REJECTED,
                ResearchProposalState.INVALID,
            },
        }
        if to_state not in allowed.get(from_state, set()):
            raise ResearchOperationsError("RESEARCH_PROPOSAL_STATE_TRANSITION_INVALID")
        expected = f"{to_state.value}:{proposal_id}:{duplicate_decision.canonical_hash}"
        if confirmation != expected:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_CONFIRMATION_INVALID")
        if (
            to_state is ResearchProposalState.APPROVED_FOR_EXPERIMENT
            and not duplicate_decision.approval_permitted
        ):
            raise ResearchOperationsError("RESEARCH_PROPOSAL_DUPLICATE_APPROVAL_FORBIDDEN")
        values = (
            _text(review_id, "RESEARCH_PROPOSAL_REVIEW_ID_INVALID", 300),
            _text(proposal_id, "RESEARCH_PROPOSAL_ID_INVALID", 300),
            ordinal,
            from_state,
            to_state,
            duplicate_decision.classification,
            duplicate_decision.canonical_hash,
            _text(actor, "RESEARCH_PROPOSAL_REVIEW_ACTOR_INVALID", 300),
            _text(reason, "RESEARCH_PROPOSAL_REVIEW_REASON_INVALID", 1000),
            confirmation,
            _time(reviewed_at, "RESEARCH_PROPOSAL_REVIEW_TIME_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest(values), "NONE")


@dataclass(frozen=True, slots=True)
class ResearchExperimentApproval:
    approval_id: str
    proposal_id: str
    proposal_version: int
    proposal_hash: str
    agenda_item_id: str
    experiment_configuration_hash: str
    required_data_ids: tuple[str, ...]
    experiment_budget: int
    review_id: str
    review_hash: str
    actor: str
    reason: str
    idempotency_key: str
    approved_at: datetime
    canonical_hash: str
    authority: str = "EXPERIMENT_ONLY"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"
    approved_experiment_id: str | None = None
    approved_data_ids: tuple[str, ...] = ()
    implementation_scope: str | None = None
    provider_request_limit: int = 0
    deferred_proposal_ids: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        proposal: ResearchProposal,
        review: ResearchProposalReview,
        idempotency_key: str,
        approved_experiment_id: str | None = None,
        approved_data_ids: tuple[str, ...] | None = None,
        implementation_scope: str | None = None,
        provider_request_limit: int = 0,
        deferred_proposal_ids: tuple[str, ...] = (),
    ) -> ResearchExperimentApproval:
        if review.proposal_id != proposal.proposal_id or review.to_state is not (
            ResearchProposalState.APPROVED_FOR_EXPERIMENT
        ):
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_APPROVAL_REVIEW_INVALID")
        if not review.actor.startswith("operator:"):
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_APPROVAL_ACTOR_INVALID")
        if provider_request_limit < 0:
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_PROVIDER_LIMIT_INVALID")
        data_ids = tuple(sorted(set(approved_data_ids or proposal.required_data_ids)))
        if not set(proposal.required_data_ids).issubset(data_ids):
            raise ResearchOperationsError("RESEARCH_EXPERIMENT_APPROVED_DATA_INVALID")
        values = (
            f"approval-{proposal.proposal_id}-v{proposal.version}",
            proposal.proposal_id,
            proposal.version,
            proposal.canonical_hash,
            proposal.agenda_item_id,
            proposal.experiment_configuration_hash,
            data_ids,
            proposal.maximum_experiment_runs,
            review.review_id,
            review.canonical_hash,
            review.actor,
            review.reason,
            _text(idempotency_key, "RESEARCH_EXPERIMENT_APPROVAL_KEY_INVALID", 160),
            review.reviewed_at,
            "EXPERIMENT_ONLY",
            "NONE",
            "NONE",
            approved_experiment_id,
            data_ids,
            implementation_scope,
            provider_request_limit,
            tuple(sorted(set(deferred_proposal_ids))),
        )
        return cls(
            approval_id=values[0],
            proposal_id=values[1],
            proposal_version=values[2],
            proposal_hash=values[3],
            agenda_item_id=values[4],
            experiment_configuration_hash=values[5],
            required_data_ids=values[6],
            experiment_budget=values[7],
            review_id=values[8],
            review_hash=values[9],
            actor=values[10],
            reason=values[11],
            idempotency_key=values[12],
            approved_at=values[13],
            canonical_hash=content_digest(values),
            authority="EXPERIMENT_ONLY",
            paper_authority="NONE",
            live_authority="NONE",
            approved_experiment_id=approved_experiment_id,
            approved_data_ids=data_ids,
            implementation_scope=implementation_scope,
            provider_request_limit=provider_request_limit,
            deferred_proposal_ids=tuple(sorted(set(deferred_proposal_ids))),
        )


@dataclass(frozen=True, slots=True)
class ResearchExperimentQueueItem:
    queue_id: str
    proposal_id: str
    proposal_hash: str
    approval_id: str
    approval_hash: str
    exact_experiment_id: str
    required_data_ids: tuple[str, ...]
    run_id: str
    execution_order: int
    idempotency_key: str
    enqueued_by: str
    enqueued_at: datetime
    canonical_hash: str
    authority: str = "EXPERIMENT_ONLY"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        queue_id: str,
        proposal: ResearchProposal,
        approval: ResearchExperimentApproval,
        exact_experiment_id: str,
        run_id: str,
        execution_order: int,
        idempotency_key: str,
        actor: str,
        enqueued_at: datetime,
    ) -> ResearchExperimentQueueItem:
        if (
            approval.proposal_id != proposal.proposal_id
            or approval.proposal_hash != proposal.canonical_hash
            or approval.experiment_configuration_hash != proposal.experiment_configuration_hash
            or not set(proposal.required_data_ids).issubset(approval.required_data_ids)
        ):
            raise ResearchOperationsError("RESEARCH_QUEUE_APPROVAL_BINDING_INVALID")
        if not 1 <= execution_order <= MAXIMUM_QUEUED_EXPERIMENTS:
            raise ResearchOperationsError("RESEARCH_QUEUE_EXECUTION_ORDER_INVALID")
        if not actor.startswith("operator:"):
            raise ResearchOperationsError("RESEARCH_QUEUE_OPERATOR_REQUIRED")
        values = (
            _text(queue_id, "RESEARCH_QUEUE_ID_INVALID", 300),
            proposal.proposal_id,
            proposal.canonical_hash,
            approval.approval_id,
            approval.canonical_hash,
            _text(exact_experiment_id, "RESEARCH_QUEUE_EXPERIMENT_ID_INVALID", 300),
            approval.required_data_ids,
            _text(run_id, "RESEARCH_RUN_ID_INVALID", 300),
            execution_order,
            _text(idempotency_key, "RESEARCH_QUEUE_IDEMPOTENCY_KEY_INVALID", 160),
            _text(actor, "RESEARCH_QUEUE_ACTOR_INVALID", 300),
            _time(enqueued_at, "RESEARCH_QUEUE_TIME_INVALID"),
            "EXPERIMENT_ONLY",
            "NONE",
            "NONE",
        )
        return cls(
            *values[:-3],
            content_digest((RESEARCH_EXPERIMENT_QUEUE_POLICY_V1, *values)),
            *values[-3:],
        )


@dataclass(frozen=True, slots=True)
class ResearchExperimentQueueEvent:
    event_id: str
    queue_id: str
    ordinal: int
    from_status: ResearchExperimentQueueStatus | None
    to_status: ResearchExperimentQueueStatus
    attempt_count: int
    budget_event_id: str | None
    result_id: str | None
    recommendation_id: str | None
    reason: str
    actor: str
    idempotency_key: str
    occurred_at: datetime
    canonical_hash: str
    authority: str = "EXPERIMENT_ONLY"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        queue_id: str,
        ordinal: int,
        from_status: ResearchExperimentQueueStatus | None,
        to_status: ResearchExperimentQueueStatus,
        attempt_count: int,
        budget_event_id: str | None,
        result_id: str | None,
        recommendation_id: str | None,
        reason: str,
        actor: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> ResearchExperimentQueueEvent:
        allowed = {
            None: {ResearchExperimentQueueStatus.QUEUED},
            ResearchExperimentQueueStatus.QUEUED: {
                ResearchExperimentQueueStatus.RUNNING,
                ResearchExperimentQueueStatus.CANCELED,
                ResearchExperimentQueueStatus.BLOCKED,
            },
            ResearchExperimentQueueStatus.RUNNING: {
                ResearchExperimentQueueStatus.COMPLETED,
                ResearchExperimentQueueStatus.FAILED,
                ResearchExperimentQueueStatus.BLOCKED,
            },
        }
        if to_status not in allowed.get(from_status, set()):
            raise ResearchOperationsError("RESEARCH_QUEUE_TRANSITION_INVALID")
        expected_attempts = (
            1
            if from_status is ResearchExperimentQueueStatus.RUNNING
            else (1 if to_status is ResearchExperimentQueueStatus.RUNNING else 0)
        )
        if ordinal <= 0 or attempt_count != expected_attempts:
            raise ResearchOperationsError("RESEARCH_QUEUE_EVENT_ORDINAL_OR_ATTEMPT_INVALID")
        values = (
            _text(event_id, "RESEARCH_QUEUE_EVENT_ID_INVALID", 300),
            _text(queue_id, "RESEARCH_QUEUE_ID_INVALID", 300),
            ordinal,
            from_status,
            to_status,
            attempt_count,
            budget_event_id,
            result_id,
            recommendation_id,
            _text(reason, "RESEARCH_QUEUE_EVENT_REASON_INVALID", 1000),
            _text(actor, "RESEARCH_QUEUE_ACTOR_INVALID", 300),
            _text(idempotency_key, "RESEARCH_QUEUE_EVENT_KEY_INVALID", 160),
            _time(occurred_at, "RESEARCH_QUEUE_EVENT_TIME_INVALID"),
            "EXPERIMENT_ONLY",
            "NONE",
            "NONE",
        )
        return cls(
            *values[:-3],
            content_digest((RESEARCH_EXPERIMENT_QUEUE_POLICY_V1, *values)),
            *values[-3:],
        )


@dataclass(frozen=True, slots=True)
class ResearchBudgetEvent:
    event_id: str
    proposal_id: str
    run_id: str
    ordinal: int
    event_type: ResearchBudgetEventType
    consumed_runs: int
    reason: str
    occurred_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        proposal_id: str,
        run_id: str,
        ordinal: int,
        event_type: ResearchBudgetEventType,
        reason: str,
        occurred_at: datetime,
    ) -> ResearchBudgetEvent:
        if ordinal <= 0:
            raise ResearchOperationsError("RESEARCH_BUDGET_EVENT_ORDINAL_INVALID")
        consumed = (
            0
            if event_type
            in {ResearchBudgetEventType.REPLAY, ResearchBudgetEventType.SUPERSEDED_DEFECT_RUN}
            else 1
            if event_type
            in {ResearchBudgetEventType.EXPERIMENT_RUN, ResearchBudgetEventType.FAILED_RUN}
            else 0
        )
        values = (
            _text(event_id, "RESEARCH_BUDGET_EVENT_ID_INVALID", 300),
            _text(proposal_id, "RESEARCH_PROPOSAL_ID_INVALID", 300),
            _text(run_id, "RESEARCH_RUN_ID_INVALID", 300),
            ordinal,
            event_type,
            consumed,
            _text(reason, "RESEARCH_BUDGET_REASON_INVALID", 1000),
            _time(occurred_at, "RESEARCH_BUDGET_TIME_INVALID"),
            "NONE",
        )
        return cls(*values[:-1], content_digest((RESEARCH_BUDGET_POLICY_V1, *values)), "NONE")


def summarize_research_budget(
    proposal: ResearchProposal,
    events: tuple[ResearchBudgetEvent, ...],
) -> ResearchBudgetSummary:
    if any(item.proposal_id != proposal.proposal_id for item in events):
        raise ResearchOperationsError("RESEARCH_BUDGET_PROPOSAL_MISMATCH")
    consumed = sum(item.consumed_runs for item in events)
    remaining = max(0, proposal.maximum_experiment_runs - consumed)
    reasons = ("EXPERIMENT_BUDGET_EXHAUSTED",) if remaining == 0 else ()
    return ResearchBudgetSummary(
        proposal_id=proposal.proposal_id,
        maximum_runs=proposal.maximum_experiment_runs,
        approved_proposals=sum(
            item.event_type is ResearchBudgetEventType.APPROVAL_RESERVED for item in events
        ),
        experiment_runs=sum(
            item.event_type is ResearchBudgetEventType.EXPERIMENT_RUN for item in events
        ),
        replays=sum(item.event_type is ResearchBudgetEventType.REPLAY for item in events),
        failed_runs=sum(item.event_type is ResearchBudgetEventType.FAILED_RUN for item in events),
        superseded_defect_runs=sum(
            item.event_type is ResearchBudgetEventType.SUPERSEDED_DEFECT_RUN for item in events
        ),
        consumed_runs=consumed,
        remaining_runs=remaining,
        stop_reasons=reasons,
    )


@dataclass(frozen=True, slots=True)
class ResearchBudgetSummary:
    proposal_id: str
    maximum_runs: int
    approved_proposals: int
    experiment_runs: int
    replays: int
    failed_runs: int
    superseded_defect_runs: int
    consumed_runs: int
    remaining_runs: int
    stop_reasons: tuple[str, ...]
    policy_id: str = RESEARCH_BUDGET_POLICY_V1


@dataclass(frozen=True, slots=True)
class ResearchExperimentOutcome:
    run_id: str
    proposal_id: str
    existing_experiment_id: str
    existing_trial_ids: tuple[str, ...]
    result_hash: str
    completed: bool
    reproducible: bool
    expected_net_economics: Decimal | None
    uncertainty_sufficient: bool
    liquidity_sufficient: bool
    downside_within_limit: bool
    robust_scope_count: int
    comparison_improved: bool
    warnings: tuple[str, ...]
    completed_at: datetime
    canonical_hash: str

    @classmethod
    def create(cls, **values: object) -> ResearchExperimentOutcome:
        net = values.get("expected_net_economics")
        if net is not None and (not isinstance(net, Decimal) or not net.is_finite()):
            raise ResearchOperationsError("RESEARCH_OUTCOME_ECONOMICS_INVALID")
        scope_count = values.get("robust_scope_count")
        if not isinstance(scope_count, int) or scope_count < 0:
            raise ResearchOperationsError("RESEARCH_OUTCOME_SCOPE_COUNT_INVALID")
        completed_at = _time(values["completed_at"], "RESEARCH_OUTCOME_TIME_INVALID")  # type: ignore[arg-type]
        normalized = {**values, "completed_at": completed_at}
        normalized["canonical_hash"] = content_digest(
            (RESEARCH_RECOMMENDATION_POLICY_V1, tuple(sorted(normalized.items())))
        )
        return cls(**normalized)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ResearchRecommendation:
    recommendation_id: str
    proposal_id: str
    run_id: str
    outcome_hash: str
    state: ResearchRecommendationState
    reasons: tuple[str, ...]
    policy_id: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    paper_authorization_created: bool = False
    live_authority: str = "NONE"


def recommend_research_outcome(outcome: ResearchExperimentOutcome) -> ResearchRecommendation:
    if not outcome.completed or not outcome.reproducible:
        state = ResearchRecommendationState.INSUFFICIENT_EVIDENCE
        reasons = ("EXPERIMENT_INCOMPLETE_OR_NOT_REPRODUCIBLE",)
    elif outcome.expected_net_economics is None or not outcome.uncertainty_sufficient:
        state = ResearchRecommendationState.INSUFFICIENT_EVIDENCE
        reasons = ("EXPECTED_ECONOMICS_OR_UNCERTAINTY_INSUFFICIENT",)
    elif outcome.expected_net_economics <= 0 or not outcome.downside_within_limit:
        state = ResearchRecommendationState.REJECT
        reasons = ("NON_POSITIVE_NET_ECONOMICS_OR_DOWNSIDE_FAILURE",)
    elif not outcome.liquidity_sufficient:
        state = ResearchRecommendationState.REVISE
        reasons = ("LIQUIDITY_EVIDENCE_INSUFFICIENT",)
    elif outcome.robust_scope_count < 2 or not outcome.comparison_improved:
        state = ResearchRecommendationState.REPLICATE
        reasons = ("ADDITIONAL_INDEPENDENT_SCOPE_OR_PRIOR_COMPARISON_REQUIRED",)
    else:
        state = ResearchRecommendationState.PAPER_CANDIDATE
        reasons = ("EXPLICIT_PAPER_REVIEW_JUSTIFIED_ONLY",)
    values = (
        f"recommendation-{outcome.run_id}",
        outcome.proposal_id,
        outcome.run_id,
        outcome.canonical_hash,
        state,
        reasons,
        RESEARCH_RECOMMENDATION_POLICY_V1,
        outcome.completed_at,
        "NONE",
        False,
        "NONE",
    )
    return ResearchRecommendation(*values[:-3], content_digest(values), *values[-3:])
