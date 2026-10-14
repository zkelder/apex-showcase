"""Application services for governed stateful multi-asset Crypto research."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from apex.application.crypto_universe import CryptoUniverseRepository
from apex.application.research_operations import (
    EmptyResearchKnowledgeSource,
    ResearchKnowledgeSource,
    ResearchOperationsRepository,
    ReviewResearchProposal,
    duplicate_decision,
)
from apex.domain.crypto_stateful_research import (
    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
    CRYPTO_STATEFUL_RESEARCH_EXECUTOR_V1,
    CryptoExecutionCandle,
    CryptoExecutionHistory,
    CryptoStatefulCurrentSignal,
    CryptoStatefulExperimentRun,
    CryptoStatefulMethodSpecification,
    CryptoStatefulResearchError,
    ResearchProposalRefinement,
    current_stateful_signal,
    run_stateful_crypto_experiment,
)
from apex.domain.crypto_universe import (
    CryptoCurrentMarketEvidence,
    CryptoProductObservation,
    CryptoResearchOpportunity,
)
from apex.domain.research_operations import (
    ResearchExperimentOutcome,
    ResearchFeasibilityState,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalFeasibility,
    ResearchProposalOrigin,
    ResearchProposalState,
)
from apex.domain.values import content_digest


class CryptoStatefulResearchRepository(Protocol):
    def refinement(self, refinement_id: str) -> ResearchProposalRefinement | None: ...
    def refinements(self) -> tuple[ResearchProposalRefinement, ...]: ...
    def save_refinement(self, value: ResearchProposalRefinement) -> bool: ...
    def specification(self, specification_id: str) -> CryptoStatefulMethodSpecification | None: ...
    def specifications(self) -> tuple[CryptoStatefulMethodSpecification, ...]: ...
    def save_specification(self, value: CryptoStatefulMethodSpecification) -> bool: ...
    def execution_history(self, history_id: str) -> CryptoExecutionHistory | None: ...
    def execution_histories(self) -> tuple[CryptoExecutionHistory, ...]: ...
    def save_execution_history(self, value: CryptoExecutionHistory) -> bool: ...
    def experiment_run(self, run_id: str) -> CryptoStatefulExperimentRun | None: ...
    def experiment_runs(self) -> tuple[CryptoStatefulExperimentRun, ...]: ...
    def save_experiment_run(self, value: CryptoStatefulExperimentRun) -> bool: ...
    def current_signal(self, signal_id: str) -> CryptoStatefulCurrentSignal | None: ...
    def current_signals(self) -> tuple[CryptoStatefulCurrentSignal, ...]: ...
    def save_current_signal(self, value: CryptoStatefulCurrentSignal) -> bool: ...


class _HasCanonicalHash(Protocol):
    @property
    def canonical_hash(self) -> str: ...


class InMemoryCryptoStatefulResearchRepository:
    def __init__(self) -> None:
        self._refinements: dict[str, ResearchProposalRefinement] = {}
        self._specifications: dict[str, CryptoStatefulMethodSpecification] = {}
        self._histories: dict[str, CryptoExecutionHistory] = {}
        self._runs: dict[str, CryptoStatefulExperimentRun] = {}
        self._signals: dict[str, CryptoStatefulCurrentSignal] = {}

    @staticmethod
    def _save[T: _HasCanonicalHash](values: dict[str, T], identity: str, value: T) -> bool:
        prior = values.get(identity)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoStatefulResearchError("CRYPTO_STATEFUL_IDENTITY_CONFLICT")
        if prior is not None:
            return False
        values[identity] = value
        return True

    def refinement(self, refinement_id: str) -> ResearchProposalRefinement | None:
        return self._refinements.get(refinement_id)

    def refinements(self) -> tuple[ResearchProposalRefinement, ...]:
        return tuple(sorted(self._refinements.values(), key=lambda value: value.refined_at))

    def save_refinement(self, value: ResearchProposalRefinement) -> bool:
        return self._save(self._refinements, value.refinement_id, value)

    def specification(self, specification_id: str) -> CryptoStatefulMethodSpecification | None:
        return self._specifications.get(specification_id)

    def specifications(self) -> tuple[CryptoStatefulMethodSpecification, ...]:
        return tuple(sorted(self._specifications.values(), key=lambda value: value.implemented_at))

    def save_specification(self, value: CryptoStatefulMethodSpecification) -> bool:
        return self._save(self._specifications, value.specification_id, value)

    def execution_history(self, history_id: str) -> CryptoExecutionHistory | None:
        return self._histories.get(history_id)

    def execution_histories(self) -> tuple[CryptoExecutionHistory, ...]:
        return tuple(sorted(self._histories.values(), key=lambda value: value.history_id))

    def save_execution_history(self, value: CryptoExecutionHistory) -> bool:
        return self._save(self._histories, value.history_id, value)

    def experiment_run(self, run_id: str) -> CryptoStatefulExperimentRun | None:
        return self._runs.get(run_id)

    def experiment_runs(self) -> tuple[CryptoStatefulExperimentRun, ...]:
        return tuple(sorted(self._runs.values(), key=lambda value: value.completed_at))

    def save_experiment_run(self, value: CryptoStatefulExperimentRun) -> bool:
        return self._save(self._runs, value.run_id, value)

    def current_signal(self, signal_id: str) -> CryptoStatefulCurrentSignal | None:
        return self._signals.get(signal_id)

    def current_signals(self) -> tuple[CryptoStatefulCurrentSignal, ...]:
        return tuple(sorted(self._signals.values(), key=lambda value: value.generated_at))

    def save_current_signal(self, value: CryptoStatefulCurrentSignal) -> bool:
        return self._save(self._signals, value.signal_id, value)


@dataclass(frozen=True, slots=True)
class RefinedCryptoProposalResult:
    proposal: ResearchProposal
    refinement: ResearchProposalRefinement
    initial_feasibility: ResearchProposalFeasibility
    approval_id: str
    idempotent_replay: bool


class RefineAndApproveGeneratedCryptoProposal:
    """Create one exact v2 proposal and one experiment-only operator approval."""

    def __init__(
        self,
        operations: ResearchOperationsRepository,
        stateful: CryptoStatefulResearchRepository,
        knowledge_source: ResearchKnowledgeSource | None = None,
    ) -> None:
        self._operations = operations
        self._stateful = stateful
        self._knowledge = knowledge_source or EmptyResearchKnowledgeSource()

    def execute(
        self,
        *,
        original_proposal_id: str,
        refined_proposal_id: str,
        refinement_id: str,
        dataset_id: str,
        retained_observation_ids: tuple[str, ...],
        current_evidence_ids: tuple[str, ...],
        actor: str,
        refined_at: datetime,
        provider_request_limit: int,
    ) -> RefinedCryptoProposalResult:
        original = self._operations.proposal(original_proposal_id)
        if original is None or original.version != 1:
            raise ResearchOperationsError("CRYPTO_STATEFUL_ORIGINAL_PROPOSAL_NOT_FOUND")
        retained = self._operations.proposal(refined_proposal_id)
        retained_refinement = self._stateful.refinement(refinement_id)
        required_data = tuple(
            sorted(set((*retained_observation_ids, *current_evidence_ids, dataset_id)))
        )
        proposal = ResearchProposal.create(
            proposal_id=refined_proposal_id,
            version=2,
            agenda_item_id=original.agenda_item_id,
            venue=original.venue,
            hypothesis=(
                "A fixed stateful one-asset pullback rotation may improve decision evidence "
                "over the retained stateless reversal-regime method without adding authority."
            ),
            expected_mechanism=(
                "Enter only a fixed three-percent short-horizon pullback inside a positive, "
                "below-median-volatility regime; exit through recovery, regime failure, "
                "quality failure, or a fourteen-day time stop, always rotating through cash."
            ),
            instruments=tuple(sorted(set(original.instruments))),
            proposed_family=original.proposed_family,
            definition_kind="CRYPTO_GENERIC_LONG_CASH",
            strategy_definition=(
                ("decision_cadence", "DAILY_COMPLETED"),
                ("execution", "NEXT_ELIGIBLE_DAILY_OPEN"),
                ("maximum_hold_days", "14"),
                ("maximum_positions", "1"),
                ("method_id", CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1),
                ("pullback_threshold", "-0.03"),
                ("return_7d", "7_COMPLETED_DAYS"),
                ("return_90d", "90_COMPLETED_DAYS"),
                ("rotation", "EXIT_TO_CASH_BEFORE_LATER_ENTRY"),
                ("state", "STATEFUL_LONG_ONE_ASSET_OR_CASH"),
                ("volatility_30d", "30_COMPLETED_DAYS_ANNUALIZED"),
                ("volatility_comparator", "ELIGIBLE_UNIVERSE_MEDIAN"),
            ),
            exact_change_from_prior=(
                "MATERIAL_SEMANTIC_REFINEMENT_ADDS_DAILY_7_30_90_LOOKBACKS_FIXED_3_PERCENT_"
                "PULLBACK_ONE_POSITION_RELATIVE_VOLATILITY_EXPLICIT_EXITS_AND_CASH_ROTATION"
            ),
            prior_research_ids=(
                original.proposal_id,
                "CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
                "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",
                "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",
                "BTC_USD_DAILY_TREND_VALIDATION_V1",
                "BTC_USD_MONTHLY_252_OBSERVATION_PRICE_TREND",
            ),
            required_data_ids=required_data,
            experiment_configuration=(
                ("dataset_id", dataset_id),
                ("execution_engine", CRYPTO_STATEFUL_RESEARCH_EXECUTOR_V1),
                ("maximum_positions", "1"),
                ("research_notional", "10000"),
                ("warmup_days", "90"),
            ),
            validation_windows=(("walk_forward", "2024-10-30/2026-08-01"),),
            cost_assumptions=(
                ("fee_bps_per_side", "60"),
                ("slippage_bps_per_side", "10"),
            ),
            success_criteria=(
                "BOUNDED_RUN_COMPLETES_WITH_EXACT_REPLAY",
                "NET_RETURN_POSITIVE_AFTER_RETAINED_COSTS",
                "DOWNSIDE_AND_COMPARISONS_RETAINED",
            ),
            failure_criteria=(
                "MISSING_REQUIRED_EVIDENCE_FAILS_CLOSED",
                "NEGATIVE_OR_UNSUPPORTED_NET_ECONOMICS",
                "NONDETERMINISTIC_REPLAY_OR_LINEAGE_MISMATCH",
            ),
            maximum_experiment_runs=1,
            origin=ResearchProposalOrigin.OPERATOR,
            actor=actor,
            warnings=(
                "ONE_PREDECLARED_CONFIGURATION_NO_PARAMETER_SEARCH",
                "NO_AUTOMATIC_PAPER_AUTHORIZATION",
                "NO_LIVE_AUTHORITY",
                "RESEARCH_ONLY",
            ),
            created_at=refined_at,
        )
        if retained is not None and retained.canonical_hash != proposal.canonical_hash:
            raise ResearchOperationsError("CRYPTO_STATEFUL_REFINED_PROPOSAL_CONFLICT")
        if retained_refinement is not None:
            if retained_refinement.refined_proposal_hash != proposal.canonical_hash:
                raise ResearchOperationsError("CRYPTO_STATEFUL_REFINEMENT_CONFLICT")
            approval = self._operations.experiment_approval(proposal.proposal_id)
            feasibility = self._operations.proposal_feasibility(proposal.proposal_id)
            if approval is None or feasibility is None:
                raise ResearchOperationsError("CRYPTO_STATEFUL_REFINEMENT_REPLAY_INCOMPLETE")
            return RefinedCryptoProposalResult(
                proposal, retained_refinement, feasibility, approval.approval_id, True
            )
        self._operations.save_proposal(proposal)
        refinement = ResearchProposalRefinement.create(
            refinement_id=refinement_id,
            original=original,
            refined=proposal,
            provider_request_limit=provider_request_limit,
            refined_at=refined_at,
        )
        self._stateful.save_refinement(refinement)
        duplicate = duplicate_decision(self._operations, proposal, self._knowledge)
        if duplicate.classification.value != "MATERIALLY_DIFFERENT_CHALLENGER":
            raise ResearchOperationsError("CRYPTO_STATEFUL_DISTINCTNESS_CLASSIFICATION_FAILED")
        initial = ResearchProposalFeasibility.create(
            feasibility_id=f"feasibility-{proposal.proposal_id}-pre-implementation",
            generator_run_id="genuine-research-proposal-generation-2026-08-02-v1",
            proposal=proposal,
            duplicate_decision=duplicate,
            schema_valid=True,
            data_available=False,
            provider_capability_available=True,
            experiment_engine_compatible=False,
            budget_available=True,
            evidence_class_compatible=True,
            authority_restrictions_passed=True,
            evaluated_at=refined_at,
        )
        if initial.status is not ResearchFeasibilityState.REQUIRES_IMPLEMENTATION:
            raise ResearchOperationsError("CRYPTO_STATEFUL_INITIAL_FEASIBILITY_INVALID")
        self._operations.save_proposal_feasibility(initial)
        reviewer = ReviewResearchProposal(self._operations, self._knowledge)
        reviewer.execute(
            proposal_id=proposal.proposal_id,
            target_state=ResearchProposalState.READY_FOR_REVIEW,
            actor=actor,
            reason="EXACT_REFINED_SEMANTICS_AND_MATERIAL_DISTINCTNESS_REVIEWED",
            confirmation=f"READY_FOR_REVIEW:{proposal.proposal_id}:{duplicate.canonical_hash}",
            reviewed_at=refined_at + timedelta(seconds=1),
        )
        review, _, _ = reviewer.execute(
            proposal_id=proposal.proposal_id,
            target_state=ResearchProposalState.APPROVED_FOR_EXPERIMENT,
            actor=actor,
            reason=("APPROVE_ONE_FIXED_STATEFUL_EXPERIMENT_AND_BOUNDED_OPEN_EVIDENCE_COMPLETION"),
            confirmation=(
                f"APPROVED_FOR_EXPERIMENT:{proposal.proposal_id}:{duplicate.canonical_hash}"
            ),
            reviewed_at=refined_at + timedelta(seconds=2),
            idempotency_key=f"{refinement_id}-approval",
            approved_experiment_id=CRYPTO_STATEFUL_RESEARCH_EXECUTOR_V1,
            approved_data_ids=required_data,
            implementation_scope="REUSABLE_ONE_POSITION_STATEFUL_CRYPTO_RESEARCH_PRIMITIVE",
            provider_request_limit=provider_request_limit,
            deferred_proposal_ids=tuple(
                value.proposal_id
                for value in self._operations.proposals()
                if value.proposal_id != proposal.proposal_id
                and value.origin is not ResearchProposalOrigin.OPERATOR
                and self._operations.experiment_approval(value.proposal_id) is None
            ),
        )
        approval = self._operations.experiment_approval(proposal.proposal_id)
        if approval is None or approval.review_id != review.review_id:
            raise ResearchOperationsError("CRYPTO_STATEFUL_APPROVAL_NOT_RETAINED")
        return RefinedCryptoProposalResult(
            proposal, refinement, initial, approval.approval_id, False
        )


@dataclass(frozen=True, slots=True)
class CapturedExecutionHistoryPage:
    candles: tuple[CryptoExecutionCandle, ...]
    raw_payload: bytes
    captured_at: datetime


class CryptoExecutionHistorySource(Protocol):
    evidence_class: str
    network_access: bool

    def capture(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedExecutionHistoryPage: ...


@dataclass(frozen=True, slots=True)
class CryptoExecutionDatasetCapture:
    dataset_id: str
    histories: tuple[CryptoExecutionHistory, ...]
    request_count: int
    canonical_hash: str
    idempotent_replay: bool


class CaptureCryptoExecutionHistories:
    """Complete only missing daily-open evidence under the exact approval bound."""

    def __init__(
        self,
        operations: ResearchOperationsRepository,
        stateful: CryptoStatefulResearchRepository,
        source: CryptoExecutionHistorySource,
    ) -> None:
        self._operations = operations
        self._stateful = stateful
        self._source = source

    def execute(
        self,
        *,
        proposal_id: str,
        dataset_id: str,
        observations: tuple[CryptoProductObservation, ...],
        requested_at: datetime,
    ) -> CryptoExecutionDatasetCapture:
        approval = self._operations.experiment_approval(proposal_id)
        if approval is None or approval.provider_request_limit <= 0:
            raise ResearchOperationsError("CRYPTO_EXECUTION_HISTORY_PROVIDER_APPROVAL_REQUIRED")
        if self._source.evidence_class != "HISTORICAL_PUBLIC":
            raise ResearchOperationsError("CRYPTO_EXECUTION_HISTORY_EVIDENCE_CLASS_INVALID")
        histories: list[CryptoExecutionHistory] = []
        request_count = 0
        replay = True
        for observation in sorted(observations, key=lambda value: value.product_id):
            history_id = f"{dataset_id}-{observation.canonical_asset_id.lower()}-open-close-v1"
            prior = self._stateful.execution_history(history_id)
            if prior is not None:
                histories.append(prior)
                continue
            replay = False
            days = (observation.interval_end - observation.interval_start).days
            partitions = (days + 299) // 300
            base, remainder = divmod(days, partitions)
            cursor = observation.interval_start
            candles: dict[datetime, CryptoExecutionCandle] = {}
            raw_hashes: list[str] = []
            captured_at = requested_at.astimezone(UTC)
            for ordinal in range(partitions):
                width = base + (1 if ordinal < remainder else 0)
                end = cursor + timedelta(days=width)
                page = self._source.capture(
                    product_id=observation.product_id,
                    interval_start=cursor,
                    interval_end=end,
                    requested_at=requested_at,
                )
                request_count += 1
                if request_count > approval.provider_request_limit:
                    raise ResearchOperationsError(
                        "CRYPTO_EXECUTION_HISTORY_PROVIDER_BOUND_EXCEEDED"
                    )
                raw_hashes.append(hashlib.sha256(page.raw_payload).hexdigest())
                captured_at = max(captured_at, page.captured_at.astimezone(UTC))
                for candle in page.candles:
                    prior_candle = candles.get(candle.bucket_start)
                    if prior_candle is not None and prior_candle != candle:
                        raise ResearchOperationsError("CRYPTO_EXECUTION_HISTORY_DUPLICATE_CONFLICT")
                    candles[candle.bucket_start] = candle
                cursor = end
            history = CryptoExecutionHistory.create(
                history_id=history_id,
                canonical_asset_id=observation.canonical_asset_id,
                product_id=observation.product_id,
                source_observation=observation,
                candles=tuple(candles.values()),
                raw_evidence_hashes=tuple(raw_hashes),
                request_count=partitions,
                captured_at=captured_at,
            )
            self._stateful.save_execution_history(history)
            histories.append(history)
        canonical_hash = content_digest(
            (dataset_id, tuple((value.history_id, value.canonical_hash) for value in histories))
        )
        return CryptoExecutionDatasetCapture(
            dataset_id, tuple(histories), request_count, canonical_hash, replay
        )


class ImplementCryptoStatefulMethod:
    def __init__(
        self,
        operations: ResearchOperationsRepository,
        stateful: CryptoStatefulResearchRepository,
        knowledge_source: ResearchKnowledgeSource | None = None,
    ) -> None:
        self._operations = operations
        self._stateful = stateful
        self._knowledge = knowledge_source or EmptyResearchKnowledgeSource()

    def execute(
        self,
        *,
        proposal_id: str,
        specification_id: str,
        implementation_id: str,
        implemented_at: datetime,
        histories: tuple[CryptoExecutionHistory, ...],
    ) -> tuple[CryptoStatefulMethodSpecification, ResearchProposalFeasibility, bool]:
        proposal = self._operations.proposal(proposal_id)
        if proposal is None:
            raise ResearchOperationsError("CRYPTO_STATEFUL_PROPOSAL_NOT_FOUND")
        prior = self._stateful.specification(specification_id)
        specification = CryptoStatefulMethodSpecification.create(
            specification_id=specification_id,
            proposal=proposal,
            implementation_id=implementation_id,
            implemented_at=implemented_at,
        )
        if prior is not None and prior.canonical_hash != specification.canonical_hash:
            raise ResearchOperationsError("CRYPTO_STATEFUL_SPECIFICATION_CONFLICT")
        created = self._stateful.save_specification(specification)
        prior_feasibility = self._operations.proposal_feasibility(proposal_id)
        if prior_feasibility is None:
            raise ResearchOperationsError("CRYPTO_STATEFUL_PRIOR_FEASIBILITY_MISSING")
        if (
            not created
            and prior_feasibility.implementation_id == implementation_id
            and prior_feasibility.status is ResearchFeasibilityState.READY_FOR_REVIEW
        ):
            return specification, prior_feasibility, True
        duplicate = duplicate_decision(self._operations, proposal, self._knowledge)
        data_available = bool(histories) and len(histories) == len(
            {value.product_id for value in histories}
        )
        feasibility = ResearchProposalFeasibility.create(
            feasibility_id=f"feasibility-{proposal_id}-implemented-v1",
            generator_run_id=prior_feasibility.generator_run_id,
            proposal=proposal,
            duplicate_decision=duplicate,
            schema_valid=True,
            data_available=data_available,
            provider_capability_available=True,
            experiment_engine_compatible=True,
            budget_available=True,
            evidence_class_compatible=all(
                value.evidence_class == "HISTORICAL_PUBLIC" for value in histories
            ),
            authority_restrictions_passed=True,
            evaluated_at=implemented_at,
            implementation_id=implementation_id,
            supersedes_feasibility_id=prior_feasibility.feasibility_id,
        )
        if feasibility.status is not ResearchFeasibilityState.READY_FOR_REVIEW:
            raise ResearchOperationsError("CRYPTO_STATEFUL_IMPLEMENTATION_NOT_READY")
        self._operations.save_proposal_feasibility(feasibility)
        return specification, feasibility, not created


class CryptoStatefulExperimentExecutor:
    """Delegate an approved queue item to the fixed reusable stateful engine."""

    def __init__(
        self,
        repository: CryptoStatefulResearchRepository,
        specification: CryptoStatefulMethodSpecification,
        histories: tuple[CryptoExecutionHistory, ...],
        dataset_id: str,
        relative_strength_comparison: Decimal | None,
        reversal_regime_comparison: Decimal | None,
    ) -> None:
        self._repository = repository
        self._specification = specification
        self._histories = histories
        self._dataset_id = dataset_id
        self._relative = relative_strength_comparison
        self._reversal = reversal_regime_comparison

    def execute(
        self, *, proposal: ResearchProposal, run_id: str, executed_at: datetime
    ) -> ResearchExperimentOutcome:
        retained = self._repository.experiment_run(run_id)
        if retained is None:
            retained = run_stateful_crypto_experiment(
                run_id=run_id,
                proposal_id=proposal.proposal_id,
                specification=self._specification,
                dataset_id=self._dataset_id,
                histories=self._histories,
                relative_strength_comparison=self._relative,
                reversal_regime_comparison=self._reversal,
                completed_at=executed_at,
            )
            self._repository.save_experiment_run(retained)
        metrics = retained.metrics
        return ResearchExperimentOutcome.create(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            existing_experiment_id=CRYPTO_STATEFUL_RESEARCH_EXECUTOR_V1,
            existing_trial_ids=(retained.run_id,),
            result_hash=retained.result_hash,
            completed=True,
            reproducible=True,
            expected_net_economics=metrics.net_return,
            uncertainty_sufficient=len(metrics.completed_holding_periods) >= 30,
            liquidity_sufficient=True,
            downside_within_limit=metrics.maximum_drawdown <= Decimal("0.50"),
            robust_scope_count=len(self._histories),
            comparison_improved=all(
                value is None or metrics.net_return > value
                for value in (
                    metrics.relative_strength_comparison,
                    metrics.reversal_regime_comparison,
                )
            ),
            warnings=retained.warnings,
            completed_at=executed_at,
        )


class RunCurrentCryptoStatefulMethod:
    def __init__(
        self,
        stateful: CryptoStatefulResearchRepository,
        crypto: CryptoUniverseRepository,
    ) -> None:
        self._stateful = stateful
        self._crypto = crypto

    def execute(
        self,
        *,
        signal_id: str,
        specification: CryptoStatefulMethodSpecification,
        experiment: CryptoStatefulExperimentRun,
        recommendation_id: str,
        source_pulse_id: str,
        universe_snapshot_id: str,
        observations: tuple[CryptoProductObservation, ...],
        current_market: tuple[CryptoCurrentMarketEvidence, ...],
        generated_at: datetime,
    ) -> tuple[CryptoStatefulCurrentSignal, tuple[CryptoResearchOpportunity, ...], bool]:
        retained = self._stateful.current_signal(signal_id)
        if retained is not None:
            opportunities = tuple(
                value
                for value in self._crypto.opportunities()
                if value.discovery_run_id == signal_id
            )
            return retained, opportunities, True
        signal, opportunities = current_stateful_signal(
            signal_id=signal_id,
            specification=specification,
            experiment=experiment,
            recommendation_id=recommendation_id,
            source_pulse_id=source_pulse_id,
            universe_snapshot_id=universe_snapshot_id,
            observations=observations,
            current_market=current_market,
            generated_at=generated_at,
        )
        self._stateful.save_current_signal(signal)
        self._crypto.save_opportunities(opportunities)
        return signal, opportunities, False
