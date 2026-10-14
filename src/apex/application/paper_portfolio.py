"""Bounded PAPER Autopilot orchestration.

Strategies remain pure opportunity producers.  This service is the only paper
authority that may hand accepted, retained evidence to a durable executor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from apex.application.autopilot import (
    AutopilotCycleAuthority,
    AutopilotCycleCommand,
    AutopilotCycleResult,
    AutopilotCycleState,
    AutopilotStore,
    AutopilotStrategyContext,
    InvocationState,
    RunAutopilotShadowCycle,
    ShadowAssessment,
    ShadowOpportunity,
    StrategyDescriptor,
    StrategyInvocationRecord,
    StrategyInvocationResult,
    StrategyRegistry,
)
from apex.application.evidence_review import PaperTrialAuthorization, PaperTrialUsage
from apex.domain.autopilot import OpportunityValidity, ShadowDecisionState
from apex.domain.paper import PaperExecutionEvidence, PaperExecutionPolicy, PaperPositionMark
from apex.domain.values import Money, ValueValidationError, deterministic_id


class PaperTrialCycleAuthority(Protocol):
    """Campaign-specific authority that narrows, never widens, PAPER cycles."""

    def active_trial_for(
        self,
        *,
        campaign_id: str,
        descriptor: StrategyDescriptor,
        policy: PaperExecutionPolicy,
        now: datetime,
    ) -> PaperTrialAuthorization: ...

    def record_lineage(
        self,
        *,
        authorization_id: str,
        entity_type: str,
        entity_id: str,
        now: datetime,
    ) -> None: ...


class PaperApprovedOpportunityExecutor(Protocol):
    def ensure_execution_policy(
        self, policy: PaperExecutionPolicy, *, recorded_at: datetime
    ) -> None: ...

    def execute_approved(
        self,
        *,
        campaign_id: str,
        cycle_id: str,
        assessment: ShadowAssessment,
        policy: PaperExecutionPolicy,
        recorded_at: datetime,
        paper_trial_authorization_id: str | None = None,
    ) -> None: ...


class PaperPortfolioStore(Protocol):
    def mark_open_positions(
        self, *, campaign_id: str, evidence_id: str, recorded_at: datetime
    ) -> tuple[PaperPositionMark, ...]: ...

    def portfolio_status(self, campaign_id: str) -> dict[str, object]: ...

    def strategy_scorecards(self, campaign_id: str) -> tuple[dict[str, object], ...]: ...

    def settle_position_by_fact_id(
        self,
        *,
        campaign_id: str,
        position_id: str,
        settlement_fact_id: str,
        recorded_at: datetime,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class PaperPositionMarkCommand:
    campaign_id: str
    evidence_id: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class PaperPositionSettlementCommand:
    campaign_id: str
    position_id: str
    settlement_fact_id: str
    recorded_at: datetime


class PaperPortfolioOperations:
    """Application-owned PAPER reporting and settlement entry points."""

    def __init__(self, store: PaperPortfolioStore) -> None:
        self._store = store

    def mark_positions(self, command: PaperPositionMarkCommand) -> tuple[PaperPositionMark, ...]:
        if not command.campaign_id or not command.evidence_id:
            raise ValueError("paper mark requires campaign and retained evidence identities")
        return self._store.mark_open_positions(
            campaign_id=command.campaign_id,
            evidence_id=command.evidence_id,
            recorded_at=command.recorded_at,
        )

    def status(self, campaign_id: str) -> dict[str, object]:
        if not campaign_id:
            raise ValueError("paper status requires a campaign identity")
        status = self._store.portfolio_status(campaign_id)
        return {**status, "strategy_scorecards": self._store.strategy_scorecards(campaign_id)}

    def settle_position(self, command: PaperPositionSettlementCommand) -> str:
        if not command.campaign_id or not command.position_id or not command.settlement_fact_id:
            raise ValueError("paper settlement requires campaign, position, and fact identities")
        return self._store.settle_position_by_fact_id(
            campaign_id=command.campaign_id,
            position_id=command.position_id,
            settlement_fact_id=command.settlement_fact_id,
            recorded_at=command.recorded_at,
        )


@dataclass(frozen=True, slots=True)
class PaperCycleCommand:
    cycle: AutopilotCycleCommand
    execution_policy: PaperExecutionPolicy

    def __post_init__(self) -> None:
        if self.cycle.authority is not AutopilotCycleAuthority.PAPER:
            raise ValueValidationError("PAPER cycle requires explicit PAPER authority")


def paper_evidence_reasons(
    opportunity: ShadowOpportunity,
    *,
    policy: PaperExecutionPolicy,
    cycle_at: datetime,
) -> tuple[str, ...]:
    """Reject incomplete or incompatible evidence before financial authorization."""

    evidence: PaperExecutionEvidence | None = opportunity.paper_execution_evidence
    if evidence is None:
        return ("PAPER_EVIDENCE_MISSING",)
    proposal = opportunity.proposal
    rule = opportunity.information_set.rule
    reasons: list[str] = []
    if evidence.market_observation_source_id != opportunity.market_observation_source_id:
        reasons.append("PAPER_EVIDENCE_MARKET_SOURCE_MISMATCH")
    if evidence.instrument_id != proposal.instrument_id:
        reasons.append("PAPER_EVIDENCE_INSTRUMENT_MISMATCH")
    if evidence.rule_version_id != proposal.rule_version_id:
        reasons.append("PAPER_EVIDENCE_RULE_MISMATCH")
    if evidence.rule_content_hash != rule.content_hash:
        reasons.append("PAPER_EVIDENCE_RULE_HASH_MISMATCH")
    if evidence.fee_model_version != proposal.fee_model_version:
        reasons.append("PAPER_EVIDENCE_PROPOSAL_FEE_MISMATCH")
    if evidence.fee_model_version != policy.fee_model_version:
        reasons.append("PAPER_EVIDENCE_POLICY_FEE_MISMATCH")
    if evidence.available_at > cycle_at.astimezone(UTC):
        reasons.append("PAPER_EVIDENCE_UNAVAILABLE")
    elif (
        cycle_at.astimezone(UTC) - evidence.available_at
    ).total_seconds() > policy.maximum_evidence_age_seconds:
        reasons.append("PAPER_EVIDENCE_STALE")
    if proposal.expires_at <= cycle_at.astimezone(UTC):
        reasons.append("PAPER_PROPOSAL_EXPIRED")
    if not evidence.provider or not evidence.provider_series_ticker:
        reasons.append("PAPER_EVIDENCE_PROVIDER_OR_SERIES_MISSING")
    if not evidence.asks_for(proposal.side) or not evidence.bids_for(proposal.side):
        reasons.append("PAPER_EVIDENCE_BOOK_SIDE_MISSING")
    if not set(evidence.evidence_references).issubset(opportunity.economics.evidence_references):
        reasons.append("PAPER_EVIDENCE_REFERENCE_MISMATCH")
    return tuple(sorted(reasons))


class RunAutopilotPaperCycle:
    """Run only explicit PAPER strategies, then delegate approved execution."""

    def __init__(
        self,
        store: AutopilotStore,
        registry: StrategyRegistry,
        executor: PaperApprovedOpportunityExecutor,
        trial_authority: PaperTrialCycleAuthority | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._executor = executor
        self._trial_authority = trial_authority

    def run(self, command: PaperCycleCommand) -> AutopilotCycleResult:
        cycle_command = command.cycle
        self._store.register_descriptors(self._registry.descriptors)
        self._executor.ensure_execution_policy(
            command.execution_policy, recorded_at=cycle_command.requested_at
        )
        cycle_id, campaign, policy, snapshot, replay = self._store.start_cycle(
            cycle_command, self._registry.configuration_hash
        )
        if replay:
            existing = self._store.get_cycle_result(cycle_id)
            # The durable cycle and its authorization evidence are immutable
            # before modeled execution begins.  Replaying the same command is
            # therefore also the bounded recovery path for an interruption
            # after cycle completion but before every paper execution commits.
            for assessment in existing.assessments:
                if assessment.decision is ShadowDecisionState.APPROVED:
                    self._executor.execute_approved(
                        campaign_id=cycle_command.campaign_id,
                        cycle_id=cycle_id,
                        assessment=assessment,
                        policy=command.execution_policy,
                        recorded_at=cycle_command.requested_at,
                    )
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
            campaign_id=cycle_command.campaign_id,
            decision_at=cycle_command.requested_at,
            deadline_at=cycle_command.deadline_at,
            portfolio_snapshot=snapshot,
        )
        candidates: list[ShadowOpportunity] = []
        invocation_ids: dict[str, str] = {}
        invocation_failed = False
        trial_by_strategy: dict[str, PaperTrialAuthorization] = {}
        descriptors_by_id = {item.strategy_id: item for item in self._registry.descriptors}
        try:
            # Static descriptors are immutable governance catalogue records.
            # The compatible trial resolves durable PAPER authority below, so
            # the runner can discover an approved SHADOW descriptor without
            # rewriting that catalogue row.
            paper_strategies = (
                self._registry.all_strategies(cycle_command.maximum_strategies)
                if self._trial_authority is not None
                else self._registry.paper_strategies(cycle_command.maximum_strategies)
            )
            for ordinal, strategy in enumerate(paper_strategies):
                descriptor = strategy.descriptor
                if self._trial_authority is None:
                    invocation_failed = True
                    result = StrategyInvocationResult(
                        state=InvocationState.FAILED,
                        opportunities=(),
                        evidence_references=(),
                        diagnostics=(("paper_trial", "authority-not-configured"),),
                        reason_codes=("PAPER_TRIAL_AUTHORIZATION_REQUIRED",),
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
                    continue
                if self._trial_authority is not None:
                    try:
                        trial = self._trial_authority.active_trial_for(
                            campaign_id=cycle_command.campaign_id,
                            descriptor=descriptor,
                            policy=command.execution_policy,
                            now=cycle_command.requested_at,
                        )
                        authorization_id = str(trial.authorization_id)
                        self._trial_authority.record_lineage(
                            authorization_id=authorization_id,
                            entity_type="AUTOPILOT_CYCLE",
                            entity_id=cycle_id,
                            now=cycle_command.requested_at,
                        )
                        trial_by_strategy[descriptor.strategy_id] = trial
                    except Exception:
                        # A global PAPER state is never enough.  Retain a
                        # completed, empty invocation rather than running a
                        # strategy without campaign-scoped authority.
                        invocation_failed = True
                        result = StrategyInvocationResult(
                            state=InvocationState.FAILED,
                            opportunities=(),
                            evidence_references=(),
                            diagnostics=(("paper_trial", "unavailable"),),
                            reason_codes=("PAPER_TRIAL_AUTHORIZATION_REQUIRED",),
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
                        continue
                try:
                    result = strategy.evaluate(context)
                    if len(result.opportunities) > descriptor.maximum_proposals_per_cycle:
                        raise ValueValidationError("strategy exceeded its proposal bound")
                    if (
                        len(candidates) + len(result.opportunities)
                        > cycle_command.maximum_proposals
                    ):
                        raise ValueValidationError("cycle proposal bound exceeded")
                    if result.state is InvocationState.FAILED:
                        invocation_failed = True
                    else:
                        candidates.extend(result.opportunities)
                except Exception as error:
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
                    invocation_ids.setdefault(
                        opportunity.proposal.proposal_id, invocation.invocation_id
                    )
            assessments = RunAutopilotShadowCycle._assess(
                cycle_id=cycle_id,
                campaign=campaign,
                policy=policy,
                snapshot=snapshot,
                candidates=tuple(candidates),
                invocation_ids=invocation_ids,
                now=cycle_command.requested_at,
                approved_reason="PAPER_AUTHORIZATION_ELIGIBLE",
            )
            assessments = tuple(
                replace(
                    assessment,
                    validity=OpportunityValidity.REJECTED_EVIDENCE,
                    reason_codes=reasons,
                    allocation=None,
                    risk_decision=None,
                    decision=ShadowDecisionState.REJECTED,
                )
                if assessment.decision is ShadowDecisionState.APPROVED
                and (
                    reasons := paper_evidence_reasons(
                        assessment.opportunity,
                        policy=command.execution_policy,
                        cycle_at=cycle_command.requested_at,
                    )
                )
                else assessment
                for assessment in assessments
            )
            if self._trial_authority is not None:
                constrained: list[ShadowAssessment] = []
                provisional_usage: dict[str, PaperTrialUsage] = {}
                for assessment in assessments:
                    resolved_trial = trial_by_strategy.get(
                        assessment.opportunity.economics.strategy_id
                    )
                    allocation = assessment.allocation
                    if (
                        resolved_trial is None
                        or assessment.decision is not ShadowDecisionState.APPROVED
                        or allocation is None
                    ):
                        constrained.append(assessment)
                        continue
                    limits = resolved_trial.limits
                    usage = provisional_usage.get(
                        str(resolved_trial.authorization_id), resolved_trial.usage
                    )
                    evidence = assessment.opportunity.paper_execution_evidence
                    assessment_descriptor = descriptors_by_id.get(
                        assessment.opportunity.economics.strategy_id
                    )
                    maximum_per_opportunity = (
                        campaign.contribution.amount
                        * limits.maximum_per_opportunity_risk_fraction.value
                    )
                    maximum_active_risk = (
                        campaign.contribution.amount
                        * limits.maximum_strategy_active_risk_fraction.value
                    )
                    blocked = (
                        usage.completed_cycles >= limits.maximum_paper_cycles
                        or allocation.maximum_loss.amount > maximum_per_opportunity
                        or usage.approved_opportunities >= limits.maximum_approved_opportunities
                        or usage.paper_orders >= limits.maximum_paper_orders
                        or usage.open_positions >= limits.maximum_simultaneous_open_positions
                        or usage.active_strategy_risk.amount + allocation.maximum_loss.amount
                        > maximum_active_risk
                        or usage.cumulative_authorized_risk.amount + allocation.maximum_loss.amount
                        > limits.maximum_cumulative_authorized_risk.amount
                        or assessment_descriptor is None
                        or assessment_descriptor.supported_instrument_class
                        != limits.allowed_instrument_class
                        or evidence is None
                        or evidence.provider != limits.allowed_provider
                    )
                    if blocked:
                        constrained.append(
                            replace(
                                assessment,
                                validity=OpportunityValidity.REJECTED_POLICY,
                                reason_codes=("PAPER_TRIAL_LIMIT_REACHED",),
                                allocation=None,
                                risk_decision=None,
                                decision=ShadowDecisionState.REJECTED,
                            )
                        )
                    else:
                        constrained.append(assessment)
                        provisional_usage[str(resolved_trial.authorization_id)] = replace(
                            usage,
                            approved_opportunities=usage.approved_opportunities + 1,
                            paper_orders=usage.paper_orders + 1,
                            open_positions=usage.open_positions + 1,
                            active_strategy_risk=Money(
                                usage.active_strategy_risk.amount + allocation.maximum_loss.amount
                            ),
                            cumulative_authorized_risk=Money(
                                usage.cumulative_authorized_risk.amount
                                + allocation.maximum_loss.amount
                            ),
                        )
                assessments = tuple(constrained)
            cycle_result = self._store.complete_cycle(
                cycle_id=cycle_id,
                campaign=campaign,
                portfolio_policy=policy,
                snapshot=snapshot,
                assessments=assessments,
                state=(
                    AutopilotCycleState.PARTIAL
                    if invocation_failed
                    else AutopilotCycleState.COMPLETED
                ),
                completed_at=cycle_command.requested_at,
            )
            for assessment in assessments:
                if assessment.decision is ShadowDecisionState.APPROVED:
                    resolved_trial = trial_by_strategy.get(
                        assessment.opportunity.economics.strategy_id
                    )
                    trial_id = (
                        str(resolved_trial.authorization_id) if resolved_trial is not None else None
                    )
                    if self._trial_authority is not None and trial_id is not None:
                        self._trial_authority.record_lineage(
                            authorization_id=trial_id,
                            entity_type="OPPORTUNITY_ASSESSMENT",
                            entity_id=assessment.assessment_id,
                            now=cycle_command.requested_at,
                        )
                    self._executor.execute_approved(
                        campaign_id=cycle_command.campaign_id,
                        cycle_id=cycle_id,
                        assessment=assessment,
                        policy=command.execution_policy,
                        recorded_at=cycle_command.requested_at,
                        paper_trial_authorization_id=trial_id,
                    )
            return cycle_result
        except Exception:
            self._store.fail_cycle(
                cycle_id, "PAPER_AUTOPILOT_CYCLE_FAILED", cycle_command.requested_at
            )
            raise
