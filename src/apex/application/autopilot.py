"""Durable, deterministic, shadow-only Autopilot orchestration.

Strategies are invoked outside persistence transactions.  The store receives
each invocation independently and records the final portfolio assessment in a
short, locked transaction.  No method in this module creates trade authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from apex.domain.autopilot import (
    CampaignOperatingState,
    CampaignPortfolioPolicy,
    CampaignPortfolioSnapshot,
    EvidenceTier,
    OpportunityEconomics,
    OpportunityValidity,
    ShadowDecisionState,
)
from apex.domain.campaign import Campaign, CampaignSnapshot, OperationalInvariants
from apex.domain.decisions import (
    AllocationDecision,
    CapitalAllocator,
    OpportunityProposal,
    RiskDecision,
    RiskKernel,
)
from apex.domain.market import InformationSet
from apex.domain.paper import PaperExecutionEvidence
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    ValueValidationError,
    content_digest,
    deterministic_id,
)


class StrategyFamily(StrEnum):
    EXTERNAL_PREDICTIVE = "EXTERNAL_PREDICTIVE"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    MARKET_DYNAMICS = "MARKET_DYNAMICS"
    CROSS_MARKET = "CROSS_MARKET"
    OPERATOR_SUPPLIED = "OPERATOR_SUPPLIED"
    FIXTURE_ONLY = "FIXTURE_ONLY"
    SECURITIES = "SECURITIES"


class StrategyImplementationState(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    RESEARCH = "RESEARCH"
    FIXTURE = "FIXTURE"


class StrategyAuthorityState(StrEnum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    PAUSED = "PAUSED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"


class StrategyEvidenceState(StrEnum):
    INCOMPLETE = "INCOMPLETE"
    DEPRIORITIZED = "DEPRIORITIZED"
    FIXTURE = "FIXTURE"


class InvocationState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AutopilotCycleState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class AutopilotCycleAuthority(StrEnum):
    SHADOW = "SHADOW"
    PAPER = "PAPER"


class AutopilotError(RuntimeError):
    """A bounded Autopilot request cannot be completed safely."""


class AutopilotIdempotencyConflictError(AutopilotError):
    """An idempotency key was reused with different command content."""


class CampaignControlError(AutopilotError):
    """The campaign's durable operating control blocks the requested action."""


@dataclass(frozen=True, slots=True)
class StrategyDescriptor:
    strategy_id: str
    version: str
    display_name: str
    family: StrategyFamily
    implementation_state: StrategyImplementationState
    authority_state: StrategyAuthorityState
    evidence_state: StrategyEvidenceState
    last_evidence_decision: str
    supported_instrument_class: str
    maximum_proposals_per_cycle: int
    configuration_hash: ContentHash

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("strategy ID", self.strategy_id, 160),
            ("strategy version", self.version, 160),
            ("strategy display name", self.display_name, 160),
            ("strategy evidence decision", self.last_evidence_decision, 160),
            ("supported instrument class", self.supported_instrument_class, 80),
        ):
            if not value or len(value) > maximum:
                raise ValueValidationError(f"{label} must be bounded non-empty text")
        if not 0 <= self.maximum_proposals_per_cycle <= 100:
            raise ValueValidationError("strategy proposal bound must be between zero and 100")

    @property
    def runnable(self) -> bool:
        return self.authority_state in (
            StrategyAuthorityState.SHADOW,
            StrategyAuthorityState.PAPER,
        )


@dataclass(frozen=True, slots=True)
class AutopilotStrategyContext:
    campaign_id: str
    decision_at: datetime
    deadline_at: datetime
    portfolio_snapshot: CampaignPortfolioSnapshot


@dataclass(frozen=True, slots=True)
class ShadowOpportunity:
    proposal: OpportunityProposal
    economics: OpportunityEconomics
    information_set: InformationSet
    market_observation_source_id: str
    paper_execution_evidence: PaperExecutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.proposal.proposal_id != self.economics.proposal_id:
            raise ValueValidationError("opportunity economics do not match proposal identity")
        if self.proposal.strategy_version != self.economics.strategy_version:
            raise ValueValidationError("opportunity strategy versions do not match")
        if self.proposal.instrument_id != self.economics.instrument_id:
            raise ValueValidationError("opportunity instruments do not match")
        if self.proposal.information_set_id != self.information_set.information_set_id:
            raise ValueValidationError("proposal does not match its information set")


@dataclass(frozen=True, slots=True)
class StrategyInvocationResult:
    state: InvocationState
    opportunities: tuple[ShadowOpportunity, ...]
    evidence_references: tuple[str, ...]
    diagnostics: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state is InvocationState.FAILED and self.opportunities:
            raise ValueValidationError("failed strategy invocation cannot return opportunities")
        if not self.reason_codes:
            raise ValueValidationError("strategy invocation requires at least one reason code")
        if tuple(sorted(self.diagnostics)) != self.diagnostics:
            raise ValueValidationError("strategy diagnostics must be deterministic sorted pairs")


class AutopilotStrategy(Protocol):
    descriptor: StrategyDescriptor

    def evaluate(self, context: AutopilotStrategyContext) -> StrategyInvocationResult: ...


class StrategyRegistry:
    """Static in-process strategy governance; dynamic plugin loading is absent."""

    def __init__(self, strategies: Iterable[AutopilotStrategy]) -> None:
        registrations = tuple(strategies)
        by_id: dict[str, AutopilotStrategy] = {}
        for strategy in registrations:
            descriptor = strategy.descriptor
            existing = by_id.get(descriptor.strategy_id)
            if existing is not None:
                if existing.descriptor.version != descriptor.version:
                    raise ValueValidationError("conflicting strategy versions are not permitted")
                raise ValueValidationError("duplicate strategy ID is not permitted")
            by_id[descriptor.strategy_id] = strategy
        self._strategies = tuple(by_id[key] for key in sorted(by_id))

    @property
    def descriptors(self) -> tuple[StrategyDescriptor, ...]:
        return tuple(item.descriptor for item in self._strategies)

    @property
    def configuration_hash(self) -> str:
        return content_digest(self.descriptors)

    def shadow_strategies(self, maximum: int) -> tuple[AutopilotStrategy, ...]:
        return self.authorized_strategies(StrategyAuthorityState.SHADOW, maximum)

    def paper_strategies(self, maximum: int) -> tuple[AutopilotStrategy, ...]:
        return self.authorized_strategies(StrategyAuthorityState.PAPER, maximum)

    def all_strategies(self, maximum: int) -> tuple[AutopilotStrategy, ...]:
        """Return a bounded static registry for a stricter external authority gate."""

        if maximum < 0:
            raise ValueValidationError("maximum strategies cannot be negative")
        return self._strategies[:maximum]

    def authorized_strategies(
        self, authority: StrategyAuthorityState, maximum: int
    ) -> tuple[AutopilotStrategy, ...]:
        if maximum < 0:
            raise ValueValidationError("maximum strategies cannot be negative")
        return tuple(
            strategy
            for strategy in self._strategies
            if strategy.descriptor.authority_state is authority
        )[:maximum]


def governed_strategy_descriptors() -> tuple[StrategyDescriptor, ...]:
    """Static governance metadata for research that is not an Autopilot signal.

    This catalog deliberately imports neither the public scanners nor the
    research package.  None of these entries is SHADOW-authorized in v0.1.
    """

    from apex.application.strategy_shadow import production_shadow_strategy_descriptors

    definitions = (
        (
            "kalshi-same-market-complement",
            "v1",
            "Same-market complement research",
            StrategyFamily.MARKET_STRUCTURE,
            StrategyImplementationState.IMPLEMENTED,
            StrategyAuthorityState.REJECTED,
            StrategyEvidenceState.DEPRIORITIZED,
            "STRATEGY_DEPRIORITIZE",
        ),
        (
            "kalshi-threshold-ladder",
            "v1",
            "Threshold ladder research",
            StrategyFamily.CROSS_MARKET,
            StrategyImplementationState.IMPLEMENTED,
            StrategyAuthorityState.REJECTED,
            StrategyEvidenceState.DEPRIORITIZED,
            "THRESHOLD_STRATEGY_DEPRIORITIZE",
        ),
        (
            "gfs-weather-research",
            "v1",
            "GFS weather research",
            StrategyFamily.EXTERNAL_PREDICTIVE,
            StrategyImplementationState.RESEARCH,
            StrategyAuthorityState.RESEARCH,
            StrategyEvidenceState.INCOMPLETE,
            "INSUFFICIENT_DATA",
        ),
    )
    research_descriptors = tuple(
        StrategyDescriptor(
            strategy_id=strategy_id,
            version=version,
            display_name=display_name,
            family=family,
            implementation_state=implementation_state,
            authority_state=authority_state,
            evidence_state=evidence_state,
            last_evidence_decision=last_evidence_decision,
            supported_instrument_class="BOUNDED_PREDICTION_MARKET",
            maximum_proposals_per_cycle=0,
            configuration_hash=ContentHash(
                content_digest(
                    {
                        "strategy_id": strategy_id,
                        "version": version,
                        "authority_state": authority_state,
                        "evidence_state": evidence_state,
                        "last_evidence_decision": last_evidence_decision,
                    }
                )
            ),
        )
        for (
            strategy_id,
            version,
            display_name,
            family,
            implementation_state,
            authority_state,
            evidence_state,
            last_evidence_decision,
        ) in definitions
    )
    return (*research_descriptors, *production_shadow_strategy_descriptors())


@dataclass(frozen=True, slots=True)
class AutopilotCycleCommand:
    campaign_id: str
    idempotency_key: str
    maximum_strategies: int
    maximum_proposals: int
    deadline_at: datetime
    operator_note: str
    requested_at: datetime
    authority: AutopilotCycleAuthority = AutopilotCycleAuthority.SHADOW

    def __post_init__(self) -> None:
        if not self.campaign_id or not self.idempotency_key:
            raise ValueValidationError("campaign and idempotency identifiers must not be empty")
        if not 0 <= self.maximum_strategies <= 100 or not 0 <= self.maximum_proposals <= 1000:
            raise ValueValidationError("Autopilot request bounds are invalid")
        if len(self.operator_note) > 500:
            raise ValueValidationError("operator note exceeds its bounded length")
        for label, value in (
            ("requested time", self.requested_at),
            ("cycle deadline", self.deadline_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueValidationError(f"{label} must be timezone-aware")
        requested = self.requested_at.astimezone(UTC)
        deadline = self.deadline_at.astimezone(UTC)
        if deadline <= requested:
            raise ValueValidationError("cycle deadline must be after requested time")
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "deadline_at", deadline)

    @property
    def request_hash(self) -> str:
        return content_digest(
            {
                "campaign_id": self.campaign_id,
                "idempotency_key": self.idempotency_key,
                "maximum_strategies": self.maximum_strategies,
                "maximum_proposals": self.maximum_proposals,
                "deadline_at": self.deadline_at,
                "operator_note": self.operator_note,
                "authority": self.authority,
            }
        )


@dataclass(frozen=True, slots=True)
class StrategyInvocationRecord:
    invocation_id: str
    cycle_id: str
    ordinal: int
    descriptor: StrategyDescriptor
    result: StrategyInvocationResult


@dataclass(frozen=True, slots=True)
class ShadowAssessment:
    assessment_id: str
    cycle_id: str
    invocation_id: str
    opportunity: ShadowOpportunity
    validity: OpportunityValidity
    reason_codes: tuple[str, ...]
    ranking_ordinal: int | None
    ranking_inputs: tuple[object, ...]
    allocation: AllocationDecision | None
    risk_decision: RiskDecision | None
    decision: ShadowDecisionState


@dataclass(frozen=True, slots=True)
class AutopilotCycleResult:
    cycle_id: str
    campaign_id: str
    state: AutopilotCycleState
    idempotent_replay: bool
    invocation_count: int
    assessment_count: int
    approved_count: int
    rejected_count: int
    assessments: tuple[ShadowAssessment, ...]


class AutopilotStore(Protocol):
    def register_descriptors(self, values: tuple[StrategyDescriptor, ...]) -> None: ...

    def start_cycle(
        self, command: AutopilotCycleCommand, registry_hash: str
    ) -> tuple[str, Campaign, CampaignPortfolioPolicy, CampaignPortfolioSnapshot, bool]: ...

    def record_invocation(self, value: StrategyInvocationRecord) -> None: ...

    def complete_cycle(
        self,
        *,
        cycle_id: str,
        campaign: Campaign,
        portfolio_policy: CampaignPortfolioPolicy,
        snapshot: CampaignPortfolioSnapshot,
        assessments: tuple[ShadowAssessment, ...],
        state: AutopilotCycleState,
        completed_at: datetime,
    ) -> AutopilotCycleResult: ...

    def fail_cycle(self, cycle_id: str, reason_code: str, completed_at: datetime) -> None: ...

    def get_cycle_result(self, cycle_id: str) -> AutopilotCycleResult: ...


def _validity_for(
    opportunity: ShadowOpportunity, *, now: datetime
) -> tuple[OpportunityValidity, tuple[str, ...]]:
    economics = opportunity.economics
    if economics.decision_at > now:
        return OpportunityValidity.REJECTED_OPERATIONAL, ("DECISION_TIME_IN_FUTURE",)
    if now >= opportunity.proposal.expires_at:
        return OpportunityValidity.REJECTED_EXPIRED, ("PROPOSAL_EXPIRED",)
    if economics.evidence_tier is EvidenceTier.UNVERIFIED:
        return OpportunityValidity.REJECTED_EVIDENCE, ("EVIDENCE_TIER_UNVERIFIED",)
    if economics.conservative_net_expected_value <= 0:
        return OpportunityValidity.REJECTED_NONPOSITIVE_CONSERVATIVE_VALUE, (
            "NONPOSITIVE_CONSERVATIVE_NET_EXPECTED_VALUE",
        )
    if economics.maximum_loss <= 0:
        return OpportunityValidity.REJECTED_MALFORMED, ("MAXIMUM_LOSS_NOT_POSITIVE",)
    return OpportunityValidity.VALID, ("OPPORTUNITY_VALID",)


def _ranking_key(value: ShadowOpportunity) -> tuple[object, ...]:
    economics = value.economics
    efficiency = economics.capital_efficiency
    visible = (
        economics.visible_quantity.value if economics.visible_quantity is not None else Decimal(-1)
    )
    return (
        -economics.evidence_tier.rank,
        -(1 if economics.conservative_net_expected_value > 0 else 0),
        -(efficiency if efficiency is not None else Decimal("-999999999")),
        -economics.conservative_net_expected_value,
        economics.capital_lockup_seconds,
        -visible,
        economics.strategy_id,
        economics.proposal_id,
    )


def rank_valid_opportunities(values: Iterable[ShadowOpportunity]) -> tuple[ShadowOpportunity, ...]:
    """The retained lexicographic rank; no opaque weighted score exists."""

    return tuple(sorted(values, key=_ranking_key))


def _portfolio_reasons(
    *,
    opportunity: ShadowOpportunity,
    policy: CampaignPortfolioPolicy,
    snapshot: CampaignPortfolioSnapshot,
    projected_risk: Decimal,
    projected_by_strategy: dict[str, Decimal],
    projected_by_bucket: dict[str, Decimal],
    projected_reservations: int,
) -> tuple[str, ...]:
    economics = opportunity.economics
    equity = snapshot.ledger_equity
    proposed_loss = economics.maximum_loss
    reasons: list[str] = []
    if economics.evidence_tier.rank < policy.minimum_evidence_tier.rank:
        reasons.append("PORTFOLIO_EVIDENCE_TIER_BELOW_MINIMUM")
    if economics.conservative_net_expected_value < policy.minimum_conservative_net_expected_value:
        reasons.append("PORTFOLIO_CONSERVATIVE_VALUE_BELOW_MINIMUM")
    if economics.capital_lockup_seconds > policy.maximum_capital_lockup_seconds:
        reasons.append("PORTFOLIO_MAXIMUM_LOCKUP_EXCEEDED")
    if proposed_loss > equity * policy.maximum_per_opportunity_risk_fraction.value:
        reasons.append("PORTFOLIO_PER_OPPORTUNITY_CAP_EXCEEDED")
    if (
        snapshot.active_risk + projected_risk + proposed_loss
        > equity * policy.maximum_active_risk_fraction.value
    ):
        reasons.append("PORTFOLIO_ACTIVE_RISK_CAP_EXCEEDED")
    strategy_risk = projected_by_strategy.get(economics.strategy_id, Decimal(0)) + proposed_loss
    if strategy_risk > equity * policy.maximum_per_strategy_risk_fraction.value:
        reasons.append("PORTFOLIO_STRATEGY_CAP_EXCEEDED")
    for bucket in economics.correlation_buckets:
        bucket_risk = projected_by_bucket.get(bucket, Decimal(0)) + proposed_loss
        if bucket_risk > equity * policy.maximum_per_correlation_bucket_risk_fraction.value:
            reasons.append("PORTFOLIO_CORRELATION_CAP_EXCEEDED")
            break
    if (
        snapshot.reservation_count + projected_reservations
        >= policy.maximum_simultaneous_reservations
    ):
        reasons.append("PORTFOLIO_RESERVATION_COUNT_CAP_EXCEEDED")
    if snapshot.open_position_count >= policy.maximum_simultaneous_open_positions:
        reasons.append("PORTFOLIO_OPEN_POSITION_COUNT_CAP_EXCEEDED")
    return tuple(sorted(reasons))


class RunAutopilotShadowCycle:
    """Run only SHADOW-authorized strategy definitions and retain evidence."""

    def __init__(self, store: AutopilotStore, registry: StrategyRegistry) -> None:
        self._store = store
        self._registry = registry

    def run(self, command: AutopilotCycleCommand) -> AutopilotCycleResult:
        if command.authority is not AutopilotCycleAuthority.SHADOW:
            raise AutopilotError("SHADOW service requires explicit SHADOW authority")
        self._store.register_descriptors(self._registry.descriptors)
        cycle_id, campaign, policy, snapshot, replay = self._store.start_cycle(
            command, self._registry.configuration_hash
        )
        if replay:
            existing = self._store.get_cycle_result(cycle_id)
            return AutopilotCycleResult(
                cycle_id=existing.cycle_id,
                campaign_id=existing.campaign_id,
                state=existing.state,
                idempotent_replay=True,
                invocation_count=existing.invocation_count,
                assessment_count=existing.assessment_count,
                approved_count=existing.approved_count,
                rejected_count=existing.rejected_count,
                assessments=existing.assessments,
            )
        context = AutopilotStrategyContext(
            campaign_id=command.campaign_id,
            decision_at=command.requested_at,
            deadline_at=command.deadline_at,
            portfolio_snapshot=snapshot,
        )
        candidates: list[ShadowOpportunity] = []
        candidate_invocations: dict[str, str] = {}
        invocation_failed = False
        try:
            for ordinal, strategy in enumerate(
                self._registry.shadow_strategies(command.maximum_strategies)
            ):
                descriptor = strategy.descriptor
                try:
                    result = strategy.evaluate(context)
                    if len(result.opportunities) > descriptor.maximum_proposals_per_cycle:
                        raise ValueValidationError("strategy exceeded its proposal bound")
                    if len(candidates) + len(result.opportunities) > command.maximum_proposals:
                        result = StrategyInvocationResult(
                            state=InvocationState.FAILED,
                            opportunities=(),
                            evidence_references=(),
                            diagnostics=(("proposal_bound", "cycle maximum exceeded"),),
                            reason_codes=("CYCLE_PROPOSAL_BOUND_EXCEEDED",),
                        )
                    if result.state is InvocationState.FAILED:
                        invocation_failed = True
                    else:
                        candidates.extend(result.opportunities)
                except Exception as error:  # one governed strategy cannot stop another
                    invocation_failed = True
                    result = StrategyInvocationResult(
                        state=InvocationState.FAILED,
                        opportunities=(),
                        evidence_references=(),
                        diagnostics=(("exception_type", type(error).__name__),),
                        reason_codes=("STRATEGY_INVOCATION_FAILED",),
                    )
                invocation = StrategyInvocationRecord(
                    invocation_id=deterministic_id(
                        "autopilot_invocation",
                        {"cycle_id": cycle_id, "ordinal": ordinal, "strategy": descriptor},
                    ),
                    cycle_id=cycle_id,
                    ordinal=ordinal,
                    descriptor=descriptor,
                    result=result,
                )
                self._store.record_invocation(invocation)
                for opportunity in result.opportunities:
                    candidate_invocations.setdefault(
                        opportunity.proposal.proposal_id, invocation.invocation_id
                    )
            assessments = self._assess(
                cycle_id=cycle_id,
                campaign=campaign,
                policy=policy,
                snapshot=snapshot,
                candidates=tuple(candidates),
                invocation_ids=candidate_invocations,
                now=command.requested_at,
            )
            final_state = (
                AutopilotCycleState.PARTIAL if invocation_failed else AutopilotCycleState.COMPLETED
            )
            return self._store.complete_cycle(
                cycle_id=cycle_id,
                campaign=campaign,
                portfolio_policy=policy,
                snapshot=snapshot,
                assessments=assessments,
                state=final_state,
                completed_at=command.requested_at,
            )
        except Exception:
            self._store.fail_cycle(cycle_id, "AUTOPILOT_CYCLE_FAILED", command.requested_at)
            raise

    @staticmethod
    def _assess(
        *,
        cycle_id: str,
        campaign: Campaign,
        policy: CampaignPortfolioPolicy,
        snapshot: CampaignPortfolioSnapshot,
        candidates: tuple[ShadowOpportunity, ...],
        invocation_ids: dict[str, str],
        now: datetime,
        approved_reason: str = "SHADOW_PAPER_ELIGIBLE",
    ) -> tuple[ShadowAssessment, ...]:
        seen: set[str] = set()
        invalid: list[tuple[ShadowOpportunity, OpportunityValidity, tuple[str, ...]]] = []
        valid: list[ShadowOpportunity] = []
        for candidate in candidates:
            if candidate.proposal.proposal_id in seen:
                invalid.append(
                    (candidate, OpportunityValidity.REJECTED_DUPLICATE, ("DUPLICATE_PROPOSAL",))
                )
                continue
            seen.add(candidate.proposal.proposal_id)
            validity, reasons = _validity_for(candidate, now=now)
            if validity is OpportunityValidity.VALID:
                valid.append(candidate)
            else:
                invalid.append((candidate, validity, reasons))
        ranked = rank_valid_opportunities(valid)
        result: list[ShadowAssessment] = []
        for candidate, validity, reasons in invalid:
            result.append(
                ShadowAssessment(
                    assessment_id=deterministic_id(
                        "autopilot_assessment",
                        {"cycle_id": cycle_id, "proposal_id": candidate.proposal.proposal_id},
                    ),
                    cycle_id=cycle_id,
                    invocation_id=invocation_ids[candidate.proposal.proposal_id],
                    opportunity=candidate,
                    validity=validity,
                    reason_codes=reasons,
                    ranking_ordinal=None,
                    ranking_inputs=candidate.economics.canonical_rank_inputs,
                    allocation=None,
                    risk_decision=None,
                    decision=ShadowDecisionState.REJECTED,
                )
            )
        projected_risk = Decimal(0)
        projected_by_strategy: dict[str, Decimal] = {}
        projected_by_bucket: dict[str, Decimal] = {}
        projected_reservations = 0
        campaign_snapshot = CampaignSnapshot(
            campaign=campaign,
            ledger_equity=Money(snapshot.ledger_equity),
            settled_cash=Money(snapshot.settled_cash),
            active_reservations=Money(snapshot.active_risk),
        )
        for ordinal, candidate in enumerate(ranked, start=1):
            economics = candidate.economics
            portfolio_reasons = _portfolio_reasons(
                opportunity=candidate,
                policy=policy,
                snapshot=snapshot,
                projected_risk=projected_risk,
                projected_by_strategy=projected_by_strategy,
                projected_by_bucket=projected_by_bucket,
                projected_reservations=projected_reservations,
            )
            allocation: AllocationDecision | None = None
            risk_decision: RiskDecision | None = None
            reasons = portfolio_reasons
            validity = OpportunityValidity.VALID
            decision = ShadowDecisionState.REJECTED
            if portfolio_reasons:
                validity = OpportunityValidity.REJECTED_PORTFOLIO
            else:
                visible = economics.visible_quantity or Quantity("1", "1")
                allocation = CapitalAllocator.allocate(
                    candidate.proposal,
                    campaign_snapshot,
                    visible,
                    campaign.policy,
                    decision_at=economics.decision_at,
                    expected_settlement_at=economics.capital_release_at,
                )
                risk_decision = RiskKernel.evaluate(
                    candidate.proposal,
                    allocation,
                    campaign_snapshot,
                    candidate.information_set,
                    campaign.policy,
                    OperationalInvariants(),
                    authorization_at=economics.decision_at,
                    market_observation_source_id=candidate.market_observation_source_id,
                )
                if not allocation.accepted:
                    validity = OpportunityValidity.REJECTED_POLICY
                    reasons = allocation.reason_codes
                elif not risk_decision.accepted:
                    validity = OpportunityValidity.REJECTED_OPERATIONAL
                    reasons = risk_decision.reason_codes
                else:
                    decision = ShadowDecisionState.APPROVED
                    reasons = (approved_reason,)
                    projected_loss = allocation.maximum_loss.amount
                    projected_risk += projected_loss
                    projected_reservations += 1
                    projected_by_strategy[economics.strategy_id] = (
                        projected_by_strategy.get(economics.strategy_id, Decimal(0))
                        + projected_loss
                    )
                    for bucket in economics.correlation_buckets:
                        projected_by_bucket[bucket] = (
                            projected_by_bucket.get(bucket, Decimal(0)) + projected_loss
                        )
            result.append(
                ShadowAssessment(
                    assessment_id=deterministic_id(
                        "autopilot_assessment",
                        {"cycle_id": cycle_id, "proposal_id": candidate.proposal.proposal_id},
                    ),
                    cycle_id=cycle_id,
                    invocation_id=invocation_ids[candidate.proposal.proposal_id],
                    opportunity=candidate,
                    validity=validity,
                    reason_codes=tuple(sorted(reasons)),
                    ranking_ordinal=ordinal,
                    ranking_inputs=candidate.economics.canonical_rank_inputs,
                    allocation=allocation,
                    risk_decision=risk_decision,
                    decision=decision,
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.ranking_ordinal is None,
                    item.ranking_ordinal or 0,
                    item.opportunity.proposal.proposal_id,
                ),
            )
        )


class InMemoryAutopilotStore:
    """Focused deterministic test store; it owns no financial or execution rows."""

    def __init__(
        self,
        *,
        campaign: Campaign,
        portfolio_policy: CampaignPortfolioPolicy,
        snapshot: CampaignPortfolioSnapshot,
    ) -> None:
        self._campaign = campaign
        self._portfolio_policy = portfolio_policy
        self._snapshot = snapshot
        self._state = CampaignOperatingState.RUNNING
        self._descriptors: dict[tuple[str, str], StrategyDescriptor] = {}
        self._cycles: dict[str, tuple[str, AutopilotCycleResult | None]] = {}
        self._cycle_keys: dict[str, str] = {}
        self._invocations: dict[str, StrategyInvocationRecord] = {}

    def register_descriptors(self, values: tuple[StrategyDescriptor, ...]) -> None:
        for value in values:
            key = (value.strategy_id, value.version)
            existing = self._descriptors.get(key)
            if existing is not None and existing != value:
                raise AutopilotIdempotencyConflictError(
                    "strategy registration has conflicting content"
                )
            self._descriptors[key] = value

    def start_cycle(
        self, command: AutopilotCycleCommand, registry_hash: str
    ) -> tuple[str, Campaign, CampaignPortfolioPolicy, CampaignPortfolioSnapshot, bool]:
        if command.campaign_id != self._campaign.campaign_id:
            raise AutopilotError("campaign is not initialized")
        key = command.idempotency_key
        existing = self._cycles.get(key)
        if existing is not None:
            request_hash, result = existing
            if request_hash != command.request_hash:
                raise AutopilotIdempotencyConflictError(
                    "cycle idempotency key has conflicting content"
                )
            if result is None:
                raise AutopilotError("prior cycle is still running")
            return result.cycle_id, self._campaign, self._portfolio_policy, self._snapshot, True
        if self._state is not CampaignOperatingState.RUNNING:
            raise CampaignControlError(f"campaign is {self._state.value}")
        cycle_id = deterministic_id(
            "autopilot_cycle",
            {
                "campaign_id": command.campaign_id,
                "idempotency_key": key,
                "registry_hash": registry_hash,
            },
        )
        self._cycles[key] = (command.request_hash, None)
        self._cycle_keys[cycle_id] = key
        return cycle_id, self._campaign, self._portfolio_policy, self._snapshot, False

    def record_invocation(self, value: StrategyInvocationRecord) -> None:
        existing = self._invocations.get(value.invocation_id)
        if existing is not None and existing != value:
            raise AutopilotIdempotencyConflictError("invocation has conflicting content")
        self._invocations[value.invocation_id] = value

    def complete_cycle(
        self,
        *,
        cycle_id: str,
        campaign: Campaign,
        portfolio_policy: CampaignPortfolioPolicy,
        snapshot: CampaignPortfolioSnapshot,
        assessments: tuple[ShadowAssessment, ...],
        state: AutopilotCycleState,
        completed_at: datetime,
    ) -> AutopilotCycleResult:
        _ = campaign, portfolio_policy, snapshot, completed_at
        invocations = tuple(
            item for item in self._invocations.values() if item.cycle_id == cycle_id
        )
        result = AutopilotCycleResult(
            cycle_id=cycle_id,
            campaign_id=self._campaign.campaign_id,
            state=state,
            idempotent_replay=False,
            invocation_count=len(invocations),
            assessment_count=len(assessments),
            approved_count=sum(
                item.decision is ShadowDecisionState.APPROVED for item in assessments
            ),
            rejected_count=sum(
                item.decision is ShadowDecisionState.REJECTED for item in assessments
            ),
            assessments=assessments,
        )
        key = self._cycle_keys.get(cycle_id)
        if key is None:
            raise AutopilotError("cycle is not initialized")
        request_hash, previous = self._cycles[key]
        if previous is not None:
            raise AutopilotIdempotencyConflictError("cycle was already completed")
        self._cycles[key] = (request_hash, result)
        return result

    def fail_cycle(self, cycle_id: str, reason_code: str, completed_at: datetime) -> None:
        _ = reason_code, completed_at
        key = self._cycle_keys.get(cycle_id)
        if key is None:
            return
        request_hash, result = self._cycles[key]
        if result is None:
            self._cycles[key] = (
                request_hash,
                AutopilotCycleResult(
                    cycle_id=cycle_id,
                    campaign_id=self._campaign.campaign_id,
                    state=AutopilotCycleState.FAILED,
                    idempotent_replay=False,
                    invocation_count=0,
                    assessment_count=0,
                    approved_count=0,
                    rejected_count=0,
                    assessments=(),
                ),
            )

    def get_cycle_result(self, cycle_id: str) -> AutopilotCycleResult:
        for _, result in self._cycles.values():
            if result is not None and result.cycle_id == cycle_id:
                return result
        raise AutopilotError("cycle result is unavailable")

    def control(self, state: CampaignOperatingState) -> None:
        if self._state is CampaignOperatingState.KILLED and state is CampaignOperatingState.RUNNING:
            raise CampaignControlError("killed campaign cannot be resumed")
        self._state = state
