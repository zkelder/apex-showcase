"""Candidate-independent orchestration for bounded crypto research campaigns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import median
from typing import Any, Protocol

from apex.domain.crypto_campaign_generic import (
    CampaignPolicyIntegrityFinding,
    ComparisonOperator,
    CompiledCandidatePlan,
    DecisionCadence,
    FeatureKind,
    GenericCandidateDefinition,
    GenericFeatureDefinition,
    GenericOperand,
    GenericOutcomeDecision,
    GenericPortfolio,
    GenericRuleDefinition,
    GenericRunResult,
    MissingDataAction,
    PositionState,
    RuleMode,
    compile_candidate_definition,
    execute_compiled_plan,
    generic_metric,
)
from apex.domain.crypto_dossier import (
    BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY,
    BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
    BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY,
    BTC_USD_SMA_20_100_STRATEGY_FAMILY,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.values import content_digest

GENERIC_CAMPAIGN_PROTOCOL_V1 = "GENERIC_CRYPTO_CAMPAIGN_PROTOCOL_V1"
GENERIC_CAMPAIGN_OUTCOME_V1 = "GENERIC_CRYPTO_CAMPAIGN_OUTCOME_V1"
GENERIC_CAMPAIGN_DOSSIER_V1 = "GENERIC_CRYPTO_CAMPAIGN_DOSSIER_V1"
GENERIC_CAMPAIGN_NOMINATION_V1 = "GENERIC_CRYPTO_CAMPAIGN_NOMINATION_V1"
GENERIC_CAMPAIGN_WORKFLOW_V1 = "GENERIC_CRYPTO_CAMPAIGN_WORKFLOW_V1"
_METRIC_QUANTUM = Decimal("0.00000001")

EARLIER_SNAPSHOT_ID = "btc-usd-genuine-replication-snapshot-2022-07-01-2024-07-01-v1"
LATER_SNAPSHOT_ID = "btc-usd-genuine-snapshot-2024-07-01-2026-07-01-v1"
EARLIER_WINDOW = (
    datetime(2023, 7, 1, tzinfo=UTC),
    datetime(2024, 7, 1, tzinfo=UTC),
)
LATER_WINDOW = (
    datetime(2025, 7, 1, tzinfo=UTC),
    datetime(2026, 7, 1, tzinfo=UTC),
)


class GenericCampaignConflict(RuntimeError):
    """A generic workflow identity conflicts with retained evidence."""


def _metric(value: Decimal) -> Decimal:
    return value.quantize(_METRIC_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class GenericSnapshotEvidence:
    snapshot_id: str
    manifest_hash: str
    exposure_state: str
    validation_start: datetime
    validation_end: datetime
    candles: tuple[CryptoDailyCandle, ...]


@dataclass(frozen=True, slots=True)
class GenericMaterialComparison:
    candidate_definition_id: str
    prior_strategy_id: str
    prior_semantics_hash: str
    candidate_semantics_hash: str
    materially_different: bool
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        definition: GenericCandidateDefinition,
        prior_strategy_id: str,
        prior_semantics_hash: str,
    ) -> GenericMaterialComparison:
        materially_different = definition.semantics_hash != prior_semantics_hash
        payload = (
            definition.definition_id,
            prior_strategy_id,
            prior_semantics_hash,
            definition.semantics_hash,
            materially_different,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class GenericCampaignProtocol:
    protocol_id: str
    campaign_id: str
    candidate_id: str
    definition_id: str
    plan_id: str
    plan_hash: str
    snapshot_ids: tuple[str, str]
    snapshot_manifest_hashes: tuple[str, str]
    validation_windows: tuple[tuple[datetime, datetime], tuple[datetime, datetime]]
    initial_state: PositionState
    semantics_hash: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        protocol_id: str,
        definition: GenericCandidateDefinition,
        plan: CompiledCandidatePlan,
        snapshots: tuple[GenericSnapshotEvidence, GenericSnapshotEvidence],
    ) -> GenericCampaignProtocol:
        if tuple(item.snapshot_id for item in snapshots) != definition.approved_snapshot_ids:
            raise GenericCampaignConflict("GENERIC_PROTOCOL_SNAPSHOTS_MISMATCH")
        if (
            tuple((item.validation_start, item.validation_end) for item in snapshots)
            != definition.validation_windows
        ):
            raise GenericCampaignConflict("GENERIC_PROTOCOL_WINDOWS_MISMATCH")
        if any(
            item.exposure_state not in {"FIXTURE", "EXPOSED_DEVELOPMENT_EVIDENCE"}
            for item in snapshots
        ):
            raise GenericCampaignConflict("GENERIC_PROTOCOL_EXPOSURE_UNAPPROVED")
        payload = (
            GENERIC_CAMPAIGN_PROTOCOL_V1,
            protocol_id,
            definition.campaign_id,
            definition.candidate_id,
            definition.definition_id,
            plan.plan_id,
            plan.plan_hash,
            tuple(item.snapshot_id for item in snapshots),
            tuple(item.manifest_hash for item in snapshots),
            definition.validation_windows,
            definition.initial_state,
            definition.semantics_hash,
            "NONE",
        )
        return cls(
            protocol_id,
            definition.campaign_id,
            definition.candidate_id,
            definition.definition_id,
            plan.plan_id,
            plan.plan_hash,
            (snapshots[0].snapshot_id, snapshots[1].snapshot_id),
            (snapshots[0].manifest_hash, snapshots[1].manifest_hash),
            definition.validation_windows,
            definition.initial_state,
            definition.semantics_hash,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class GenericCampaignOutcome:
    outcome_id: str
    run_id: str
    decision: GenericOutcomeDecision
    passed_conditions: tuple[str, ...]
    failed_conditions: tuple[str, ...]
    decided_at: datetime
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class GenericCampaignDossier:
    dossier_id: str
    campaign_id: str
    candidate_id: str
    definition_id: str
    plan_hash: str
    protocol_id: str
    run_ids: tuple[str, str]
    run_result_hashes: tuple[str, str]
    outcome_ids: tuple[str, str]
    complete_windows: int
    passing_windows: int
    failing_windows: int
    compounded_conservative_return: Decimal
    median_conservative_return: Decimal
    worst_maximum_drawdown: Decimal
    total_modeled_cost: Decimal
    total_entries: int
    total_exits: int
    membership_hash: str
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class GenericNominationEvaluation:
    evaluation_id: str
    dossier_id: str
    campaign_id: str
    candidate_id: str
    eligible: bool
    passed_conditions: tuple[str, ...]
    failed_conditions: tuple[str, ...]
    evaluated_at: datetime
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class GenericCampaignBudget:
    candidate_budget: int
    consumed_candidates: int
    remaining_candidates: int
    experiment_budget: int
    consumed_experiments: int
    remaining_experiments: int


@dataclass(frozen=True, slots=True)
class GenericWorkflowReport:
    workflow_id: str
    definition: GenericCandidateDefinition
    plan: CompiledCandidatePlan
    comparisons: tuple[GenericMaterialComparison, ...]
    protocol: GenericCampaignProtocol
    runs: tuple[GenericRunResult, GenericRunResult]
    outcomes: tuple[GenericCampaignOutcome, GenericCampaignOutcome]
    dossier: GenericCampaignDossier
    nomination: GenericNominationEvaluation
    budget: GenericCampaignBudget
    created_objects: tuple[str, ...]
    reused_objects: tuple[str, ...]
    holdout_accessed: bool
    network_request_performed: bool
    canonical_hash: str
    authority: str = "NONE"


class GenericCampaignRepository(Protocol):
    candidate_budget: int
    experiment_budget: int

    def definition(self, definition_id: str) -> GenericCandidateDefinition | None: ...
    def save_definition(
        self, definition: GenericCandidateDefinition
    ) -> GenericCandidateDefinition: ...
    def definitions(self) -> tuple[GenericCandidateDefinition, ...]: ...
    def plan(self, plan_id: str) -> CompiledCandidatePlan | None: ...
    def save_plan(self, plan: CompiledCandidatePlan) -> CompiledCandidatePlan: ...
    def plans(self) -> tuple[CompiledCandidatePlan, ...]: ...
    def protocol(self, protocol_id: str) -> GenericCampaignProtocol | None: ...
    def save_protocol(self, protocol: GenericCampaignProtocol) -> GenericCampaignProtocol: ...
    def run(self, run_id: str) -> GenericRunResult | None: ...
    def save_run(self, run: GenericRunResult) -> GenericRunResult: ...
    def outcome(self, outcome_id: str) -> GenericCampaignOutcome | None: ...
    def save_outcome(self, outcome: GenericCampaignOutcome) -> GenericCampaignOutcome: ...
    def dossier(self, dossier_id: str) -> GenericCampaignDossier | None: ...
    def save_dossier(self, dossier: GenericCampaignDossier) -> GenericCampaignDossier: ...
    def nomination(self, evaluation_id: str) -> GenericNominationEvaluation | None: ...
    def save_nomination(
        self, nomination: GenericNominationEvaluation
    ) -> GenericNominationEvaluation: ...
    def consumed_candidate_ids(self) -> tuple[str, ...]: ...
    def consume_candidate(self, candidate_id: str) -> None: ...
    def consumed_run_ids(self) -> tuple[str, ...]: ...
    def consume_experiment(self, run_id: str) -> None: ...
    def idempotency_value(self, key: str) -> str | None: ...
    def save_idempotency_value(self, key: str, canonical_hash: str) -> None: ...


class _CanonicalValue(Protocol):
    @property
    def canonical_hash(self) -> str: ...


class InMemoryGenericCampaignRepository:
    """Isolated repository used for fixture acceptance and pure orchestration proofs."""

    def __init__(self, *, candidate_budget: int = 3, experiment_budget: int = 6) -> None:
        self.candidate_budget = candidate_budget
        self.experiment_budget = experiment_budget
        self._definitions: dict[str, GenericCandidateDefinition] = {}
        self._plans: dict[str, CompiledCandidatePlan] = {}
        self._protocols: dict[str, GenericCampaignProtocol] = {}
        self._runs: dict[str, GenericRunResult] = {}
        self._outcomes: dict[str, GenericCampaignOutcome] = {}
        self._dossiers: dict[str, GenericCampaignDossier] = {}
        self._nominations: dict[str, GenericNominationEvaluation] = {}
        self._candidate_ids: set[str] = set()
        self._run_ids: set[str] = set()
        self._idempotency: dict[str, str] = {}

    @staticmethod
    def _save[T: _CanonicalValue](
        values: dict[str, T],
        identity: str,
        value: T,
        canonical_hash: str,
        conflict: str,
    ) -> T:
        prior = values.get(identity)
        if prior is not None and prior.canonical_hash != canonical_hash:
            raise GenericCampaignConflict(conflict)
        values[identity] = prior or value
        return prior or value

    def definition(self, definition_id: str) -> GenericCandidateDefinition | None:
        return self._definitions.get(definition_id)

    def save_definition(self, definition: GenericCandidateDefinition) -> GenericCandidateDefinition:
        return self._save(
            self._definitions,
            definition.definition_id,
            definition,
            definition.canonical_hash,
            "GENERIC_DEFINITION_CONFLICT",
        )

    def definitions(self) -> tuple[GenericCandidateDefinition, ...]:
        return tuple(sorted(self._definitions.values(), key=lambda item: item.definition_id))

    def plan(self, plan_id: str) -> CompiledCandidatePlan | None:
        return self._plans.get(plan_id)

    def save_plan(self, plan: CompiledCandidatePlan) -> CompiledCandidatePlan:
        return self._save(
            self._plans,
            plan.plan_id,
            plan,
            plan.canonical_hash,
            "GENERIC_PLAN_CONFLICT",
        )

    def plans(self) -> tuple[CompiledCandidatePlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda item: item.plan_id))

    def protocol(self, protocol_id: str) -> GenericCampaignProtocol | None:
        return self._protocols.get(protocol_id)

    def save_protocol(self, protocol: GenericCampaignProtocol) -> GenericCampaignProtocol:
        return self._save(
            self._protocols,
            protocol.protocol_id,
            protocol,
            protocol.canonical_hash,
            "GENERIC_PROTOCOL_CONFLICT",
        )

    def run(self, run_id: str) -> GenericRunResult | None:
        return self._runs.get(run_id)

    def save_run(self, run: GenericRunResult) -> GenericRunResult:
        return self._save(
            self._runs,
            run.run_id,
            run,
            run.canonical_hash,
            "GENERIC_RUN_CONFLICT",
        )

    def outcome(self, outcome_id: str) -> GenericCampaignOutcome | None:
        return self._outcomes.get(outcome_id)

    def save_outcome(self, outcome: GenericCampaignOutcome) -> GenericCampaignOutcome:
        return self._save(
            self._outcomes,
            outcome.outcome_id,
            outcome,
            outcome.canonical_hash,
            "GENERIC_OUTCOME_CONFLICT",
        )

    def dossier(self, dossier_id: str) -> GenericCampaignDossier | None:
        return self._dossiers.get(dossier_id)

    def save_dossier(self, dossier: GenericCampaignDossier) -> GenericCampaignDossier:
        return self._save(
            self._dossiers,
            dossier.dossier_id,
            dossier,
            dossier.canonical_hash,
            "GENERIC_DOSSIER_CONFLICT",
        )

    def nomination(self, evaluation_id: str) -> GenericNominationEvaluation | None:
        return self._nominations.get(evaluation_id)

    def save_nomination(
        self, nomination: GenericNominationEvaluation
    ) -> GenericNominationEvaluation:
        return self._save(
            self._nominations,
            nomination.evaluation_id,
            nomination,
            nomination.canonical_hash,
            "GENERIC_NOMINATION_CONFLICT",
        )

    def consumed_candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._candidate_ids))

    def consume_candidate(self, candidate_id: str) -> None:
        if candidate_id in self._candidate_ids:
            return
        if len(self._candidate_ids) >= self.candidate_budget:
            raise GenericCampaignConflict("GENERIC_CANDIDATE_BUDGET_EXHAUSTED")
        self._candidate_ids.add(candidate_id)

    def consumed_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._run_ids))

    def consume_experiment(self, run_id: str) -> None:
        if run_id in self._run_ids:
            return
        if len(self._run_ids) >= self.experiment_budget:
            raise GenericCampaignConflict("GENERIC_EXPERIMENT_BUDGET_EXHAUSTED")
        self._run_ids.add(run_id)

    def idempotency_value(self, key: str) -> str | None:
        return self._idempotency.get(key)

    def save_idempotency_value(self, key: str, canonical_hash: str) -> None:
        prior = self._idempotency.get(key)
        if prior is not None and prior != canonical_hash:
            raise GenericCampaignConflict("GENERIC_IDEMPOTENCY_CONFLICT")
        self._idempotency[key] = prior or canonical_hash


def _window_conditions(
    definition: GenericCandidateDefinition, run: GenericRunResult
) -> tuple[tuple[str, bool], ...]:
    candidate = generic_metric(run, GenericPortfolio.CANDIDATE)
    buy_hold = generic_metric(run, GenericPortfolio.BUY_AND_HOLD)
    values = {
        "CONSERVATIVE_NET_RETURN_AT_LEAST_ZERO": candidate.net_return >= 0,
        "DRAWDOWN_STRICTLY_BELOW_BUY_AND_HOLD": (
            candidate.maximum_drawdown < buy_hold.maximum_drawdown
        ),
        "EVIDENCE_COMPLETE": run.complete,
        "REPRODUCIBLE": run.reproducibility_status.value == "REPRODUCIBLE",
        "NO_DISQUALIFYING_WARNING": not run.disqualifying_warnings,
        "AT_LEAST_ONE_LONG_EXPOSURE_PERIOD": candidate.days_long > 0,
    }
    return tuple((item, values.get(item, False)) for item in definition.per_window_policy)


class GenericCampaignWorkflow:
    """One bounded stepwise and orchestrated workflow with no candidate identity branches."""

    def __init__(self, repository: GenericCampaignRepository) -> None:
        self._repository = repository

    def validate(self, definition: GenericCandidateDefinition) -> GenericCandidateDefinition:
        compile_candidate_definition(definition)
        return definition

    def compile(self, definition: GenericCandidateDefinition) -> CompiledCandidatePlan:
        return compile_candidate_definition(definition)

    def register(
        self,
        *,
        definition: GenericCandidateDefinition,
        prior_semantics: tuple[tuple[str, str], ...],
    ) -> tuple[
        GenericCandidateDefinition,
        CompiledCandidatePlan,
        tuple[GenericMaterialComparison, ...],
        bool,
    ]:
        self.validate(definition)
        comparisons = tuple(
            GenericMaterialComparison.create(
                definition=definition,
                prior_strategy_id=identity,
                prior_semantics_hash=semantics,
            )
            for identity, semantics in prior_semantics
        )
        if any(not item.materially_different for item in comparisons):
            raise GenericCampaignConflict("GENERIC_DUPLICATE_SEMANTICS_ALREADY_TESTED")
        prior = self._repository.definition(definition.definition_id)
        retained_definition = self._repository.save_definition(definition)
        plan = self._repository.save_plan(self.compile(definition))
        created = prior is None
        if created:
            self._repository.consume_candidate(definition.candidate_id)
        return retained_definition, plan, comparisons, created

    def register_protocol(
        self,
        *,
        protocol_id: str,
        definition: GenericCandidateDefinition,
        plan: CompiledCandidatePlan,
        snapshots: tuple[GenericSnapshotEvidence, GenericSnapshotEvidence],
    ) -> tuple[GenericCampaignProtocol, bool]:
        value = GenericCampaignProtocol.create(
            protocol_id=protocol_id,
            definition=definition,
            plan=plan,
            snapshots=snapshots,
        )
        prior = self._repository.protocol(protocol_id)
        return self._repository.save_protocol(value), prior is None

    def execute_window(
        self,
        *,
        definition: GenericCandidateDefinition,
        plan: CompiledCandidatePlan,
        protocol: GenericCampaignProtocol,
        snapshot: GenericSnapshotEvidence,
        run_id: str,
        executed_at: datetime,
    ) -> tuple[GenericRunResult, bool]:
        value = execute_compiled_plan(
            plan=plan,
            definition=definition,
            run_id=run_id,
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_manifest_hash=snapshot.manifest_hash,
            snapshot_exposure_state=snapshot.exposure_state,
            validation_start=snapshot.validation_start,
            validation_end=snapshot.validation_end,
            candles=snapshot.candles,
            executed_at=executed_at,
        )
        prior = self._repository.run(run_id)
        retained = self._repository.save_run(value)
        created = prior is None
        if created:
            self._repository.consume_experiment(run_id)
        return retained, created

    def replay(
        self,
        *,
        definition: GenericCandidateDefinition,
        plan: CompiledCandidatePlan,
        protocol: GenericCampaignProtocol,
        snapshot: GenericSnapshotEvidence,
        run_id: str,
    ) -> GenericRunResult:
        retained = self._repository.run(run_id)
        if retained is None:
            raise GenericCampaignConflict("GENERIC_RUN_NOT_FOUND")
        replay = execute_compiled_plan(
            plan=plan,
            definition=definition,
            run_id=run_id,
            protocol_id=protocol.protocol_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_manifest_hash=snapshot.manifest_hash,
            snapshot_exposure_state=snapshot.exposure_state,
            validation_start=snapshot.validation_start,
            validation_end=snapshot.validation_end,
            candles=snapshot.candles,
            executed_at=retained.executed_at,
        )
        if (
            replay.result_hash != retained.result_hash
            or replay.canonical_hash != retained.canonical_hash
        ):
            raise GenericCampaignConflict("GENERIC_REPLAY_DIVERGENCE")
        return replay

    def record_outcome(
        self,
        *,
        outcome_id: str,
        definition: GenericCandidateDefinition,
        run: GenericRunResult,
        decided_at: datetime,
    ) -> GenericCampaignOutcome:
        conditions = _window_conditions(definition, run)
        passed = tuple(name for name, value in conditions if value)
        failed = tuple(name for name, value in conditions if not value)
        if not run.complete:
            decision = GenericOutcomeDecision.INCONCLUSIVE
        elif failed:
            decision = GenericOutcomeDecision.REJECTED
        else:
            decision = GenericOutcomeDecision.PROMOTION_CANDIDATE
        payload = (
            GENERIC_CAMPAIGN_OUTCOME_V1,
            outcome_id,
            run.run_id,
            decision,
            passed,
            failed,
            decided_at,
            "NONE",
        )
        return self._repository.save_outcome(
            GenericCampaignOutcome(
                outcome_id,
                run.run_id,
                decision,
                passed,
                failed,
                decided_at,
                content_digest(payload),
            )
        )

    def materialize_dossier(
        self,
        *,
        dossier_id: str,
        definition: GenericCandidateDefinition,
        plan: CompiledCandidatePlan,
        protocol: GenericCampaignProtocol,
        runs: tuple[GenericRunResult, GenericRunResult],
        outcomes: tuple[GenericCampaignOutcome, GenericCampaignOutcome],
    ) -> GenericCampaignDossier:
        metrics = tuple(generic_metric(item, GenericPortfolio.CANDIDATE) for item in runs)
        returns = tuple(item.net_return for item in metrics)
        passing = sum(
            item.decision is GenericOutcomeDecision.PROMOTION_CANDIDATE for item in outcomes
        )
        membership = content_digest(
            (
                definition.canonical_hash,
                plan.plan_hash,
                protocol.canonical_hash,
                tuple(item.result_hash for item in runs),
                tuple(item.canonical_hash for item in outcomes),
            )
        )
        payload = (
            GENERIC_CAMPAIGN_DOSSIER_V1,
            dossier_id,
            definition.campaign_id,
            definition.candidate_id,
            definition.definition_id,
            plan.plan_hash,
            protocol.protocol_id,
            tuple(item.run_id for item in runs),
            tuple(item.result_hash for item in runs),
            tuple(item.outcome_id for item in outcomes),
            sum(item.complete for item in runs),
            passing,
            len(runs) - passing,
            _metric((Decimal(1) + returns[0]) * (Decimal(1) + returns[1]) - Decimal(1)),
            _metric(Decimal(str(median(returns)))),
            max(item.maximum_drawdown for item in metrics),
            sum((item.total_modeled_cost for item in metrics), Decimal(0)),
            sum(item.entries for item in metrics),
            sum(item.exits for item in metrics),
            membership,
        )
        value = GenericCampaignDossier(
            dossier_id,
            definition.campaign_id,
            definition.candidate_id,
            definition.definition_id,
            plan.plan_hash,
            protocol.protocol_id,
            (runs[0].run_id, runs[1].run_id),
            (runs[0].result_hash, runs[1].result_hash),
            (outcomes[0].outcome_id, outcomes[1].outcome_id),
            sum(item.complete for item in runs),
            passing,
            len(runs) - passing,
            _metric((Decimal(1) + returns[0]) * (Decimal(1) + returns[1]) - Decimal(1)),
            _metric(Decimal(str(median(returns)))),
            max(item.maximum_drawdown for item in metrics),
            sum((item.total_modeled_cost for item in metrics), Decimal(0)),
            sum(item.entries for item in metrics),
            sum(item.exits for item in metrics),
            membership,
            content_digest(payload),
        )
        return self._repository.save_dossier(value)

    def evaluate_nomination(
        self,
        *,
        evaluation_id: str,
        definition: GenericCandidateDefinition,
        dossier: GenericCampaignDossier,
        evaluated_at: datetime,
    ) -> GenericNominationEvaluation:
        conditions = (
            ("EVERY_WINDOW_PASSES", dossier.passing_windows == 2),
            (
                "COMPOUNDED_CONSERVATIVE_RETURN_POSITIVE",
                dossier.compounded_conservative_return > 0,
            ),
            ("SEALED_HOLDOUT_NOT_ACCESSED", True),
            ("AUTHORITY_REMAINS_NONE", True),
        )
        passed = tuple(name for name, value in conditions if value)
        failed = tuple(name for name, value in conditions if not value)
        payload = (
            GENERIC_CAMPAIGN_NOMINATION_V1,
            evaluation_id,
            dossier.dossier_id,
            definition.campaign_id,
            definition.candidate_id,
            not failed,
            passed,
            failed,
            evaluated_at,
            "NONE",
        )
        value = GenericNominationEvaluation(
            evaluation_id=evaluation_id,
            dossier_id=dossier.dossier_id,
            campaign_id=definition.campaign_id,
            candidate_id=definition.candidate_id,
            eligible=not failed,
            passed_conditions=passed,
            failed_conditions=failed,
            evaluated_at=evaluated_at,
            canonical_hash=content_digest(payload),
        )
        return self._repository.save_nomination(value)

    def budget(self) -> GenericCampaignBudget:
        candidates = len(self._repository.consumed_candidate_ids())
        experiments = len(self._repository.consumed_run_ids())
        return GenericCampaignBudget(
            self._repository.candidate_budget,
            candidates,
            self._repository.candidate_budget - candidates,
            self._repository.experiment_budget,
            experiments,
            self._repository.experiment_budget - experiments,
        )

    def orchestrate(
        self,
        *,
        workflow_id: str,
        definition: GenericCandidateDefinition,
        prior_semantics: tuple[tuple[str, str], ...],
        protocol_id: str,
        snapshots: tuple[GenericSnapshotEvidence, GenericSnapshotEvidence],
        run_ids: tuple[str, str],
        outcome_ids: tuple[str, str],
        dossier_id: str,
        evaluation_id: str,
        executed_at: datetime,
        idempotency_key: str,
    ) -> GenericWorkflowReport:
        if not idempotency_key.strip():
            raise GenericCampaignConflict("GENERIC_IDEMPOTENCY_KEY_REQUIRED")
        created: list[str] = []
        reused: list[str] = []
        retained_definition, plan, comparisons, definition_created = self.register(
            definition=definition,
            prior_semantics=prior_semantics,
        )
        (created if definition_created else reused).extend(
            (retained_definition.definition_id, plan.plan_id)
        )
        protocol, protocol_created = self.register_protocol(
            protocol_id=protocol_id,
            definition=retained_definition,
            plan=plan,
            snapshots=snapshots,
        )
        (created if protocol_created else reused).append(protocol.protocol_id)
        run_values: list[GenericRunResult] = []
        outcome_values: list[GenericCampaignOutcome] = []
        for index, snapshot in enumerate(snapshots):
            run, run_created = self.execute_window(
                definition=retained_definition,
                plan=plan,
                protocol=protocol,
                snapshot=snapshot,
                run_id=run_ids[index],
                executed_at=executed_at,
            )
            (created if run_created else reused).append(run.run_id)
            run_values.append(run)
            prior_outcome = self._repository.outcome(outcome_ids[index])
            outcome = self.record_outcome(
                outcome_id=outcome_ids[index],
                definition=retained_definition,
                run=run,
                decided_at=executed_at,
            )
            (reused if prior_outcome is not None else created).append(outcome.outcome_id)
            outcome_values.append(outcome)
        concrete_runs = (run_values[0], run_values[1])
        concrete_outcomes = (outcome_values[0], outcome_values[1])
        prior_dossier = self._repository.dossier(dossier_id)
        dossier = self.materialize_dossier(
            dossier_id=dossier_id,
            definition=retained_definition,
            plan=plan,
            protocol=protocol,
            runs=concrete_runs,
            outcomes=concrete_outcomes,
        )
        (reused if prior_dossier is not None else created).append(dossier.dossier_id)
        prior_nomination = self._repository.nomination(evaluation_id)
        nomination = self.evaluate_nomination(
            evaluation_id=evaluation_id,
            definition=retained_definition,
            dossier=dossier,
            evaluated_at=executed_at,
        )
        (reused if prior_nomination is not None else created).append(nomination.evaluation_id)
        report_payload = (
            GENERIC_CAMPAIGN_WORKFLOW_V1,
            workflow_id,
            retained_definition.canonical_hash,
            plan.canonical_hash,
            comparisons,
            protocol.canonical_hash,
            tuple(item.canonical_hash for item in concrete_runs),
            tuple(item.canonical_hash for item in concrete_outcomes),
            dossier.canonical_hash,
            nomination.canonical_hash,
            self.budget(),
            False,
            False,
            "NONE",
        )
        report = GenericWorkflowReport(
            workflow_id,
            retained_definition,
            plan,
            comparisons,
            protocol,
            concrete_runs,
            concrete_outcomes,
            dossier,
            nomination,
            self.budget(),
            tuple(sorted(created)),
            tuple(sorted(reused)),
            False,
            False,
            content_digest(report_payload),
        )
        prior_hash = self._repository.idempotency_value(idempotency_key)
        if prior_hash is not None and prior_hash != report.canonical_hash:
            raise GenericCampaignConflict("GENERIC_WORKFLOW_IDEMPOTENCY_CONFLICT")
        self._repository.save_idempotency_value(idempotency_key, report.canonical_hash)
        return report


@dataclass(frozen=True, slots=True)
class GenericCompatibilityProjection:
    historical_strategy_id: str
    historical_family: str
    historical_semantics_hash: str
    definition: GenericCandidateDefinition
    plan: CompiledCandidatePlan
    economic_rules_representable: bool
    historical_hash_claim: str
    canonical_hash: str
    authority: str = "NONE"


def _compatibility_definition(
    *,
    identity: str,
    family: str,
    cadence: DecisionCadence,
    mode: RuleMode,
    features: tuple[GenericFeatureDefinition, ...],
    rules: tuple[GenericRuleDefinition, ...],
    version: int,
) -> GenericCandidateDefinition:
    return GenericCandidateDefinition.create(
        definition_id=f"{identity}-generic-compatibility-v1",
        campaign_id="btc-usd-risk-managed-long-cash-campaign-v1",
        candidate_id=f"{identity}-compatibility-candidate",
        hypothesis_id=f"{identity}-compatibility-hypothesis",
        hypothesis_version_id=f"{identity}-compatibility-hypothesis-v1",
        strategy_specification_id=identity,
        strategy_version=version,
        family=family,
        expected_mechanism=(
            "Read-only compatibility projection of retained economic rules; "
            "it does not register a campaign candidate or rewrite evidence."
        ),
        approved_snapshot_ids=(EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID),
        validation_windows=(EARLIER_WINDOW, LATER_WINDOW),
        cadence=cadence,
        initial_state=PositionState.CASH,
        rule_mode=mode,
        features=features,
        rules=rules,
    )


def retained_strategy_compatibility() -> tuple[GenericCompatibilityProjection, ...]:
    mean20 = GenericFeatureDefinition.create(
        name="mean_20",
        kind=FeatureKind.ROLLING_MEAN,
        observations=20,
        exclude_decision_observation=False,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    mean100 = GenericFeatureDefinition.create(
        name="mean_100",
        kind=FeatureKind.ROLLING_MEAN,
        observations=100,
        exclude_decision_observation=False,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    sma_rule = GenericRuleDefinition.create(
        rule_id="fast_above_slow",
        source_state=None,
        left=GenericOperand.feature("mean_20"),
        operator=ComparisonOperator.STRICTLY_GREATER_THAN,
        right=GenericOperand.feature("mean_100"),
        trigger_state=PositionState.LONG,
        non_trigger_state=PositionState.CASH,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    return252 = GenericFeatureDefinition.create(
        name="return_252",
        kind=FeatureKind.POINT_TO_POINT_RETURN,
        observations=253,
        exclude_decision_observation=False,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    momentum_rule = GenericRuleDefinition.create(
        rule_id="positive_252_return",
        source_state=None,
        left=GenericOperand.feature("return_252"),
        operator=ComparisonOperator.STRICTLY_GREATER_THAN,
        right=GenericOperand.constant_value("0"),
        trigger_state=PositionState.LONG,
        non_trigger_state=PositionState.CASH,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    mean200 = GenericFeatureDefinition.create(
        name="mean_200",
        kind=FeatureKind.ROLLING_MEAN,
        observations=200,
        exclude_decision_observation=False,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    mean_rule = GenericRuleDefinition.create(
        rule_id="close_above_mean_200",
        source_state=None,
        left=GenericOperand.decision_close(),
        operator=ComparisonOperator.STRICTLY_GREATER_THAN,
        right=GenericOperand.feature("mean_200"),
        trigger_state=PositionState.LONG,
        non_trigger_state=PositionState.CASH,
        missing_data_action=MissingDataAction.SELECT_CASH,
    )
    maximum90 = GenericFeatureDefinition.create(
        name="maximum_90",
        kind=FeatureKind.ROLLING_MAXIMUM,
        observations=90,
        exclude_decision_observation=True,
        missing_data_action=MissingDataAction.PRESERVE_CASH,
    )
    minimum45 = GenericFeatureDefinition.create(
        name="minimum_45",
        kind=FeatureKind.ROLLING_MINIMUM,
        observations=45,
        exclude_decision_observation=True,
        missing_data_action=MissingDataAction.PROTECTIVE_EXIT_TO_CASH,
    )
    entry_rule = GenericRuleDefinition.create(
        rule_id="entry_90_close_maximum",
        source_state=PositionState.CASH,
        left=GenericOperand.decision_close(),
        operator=ComparisonOperator.STRICTLY_GREATER_THAN,
        right=GenericOperand.feature("maximum_90"),
        trigger_state=PositionState.LONG,
        non_trigger_state=PositionState.CASH,
        missing_data_action=MissingDataAction.PRESERVE_CASH,
    )
    exit_rule = GenericRuleDefinition.create(
        rule_id="exit_45_close_minimum",
        source_state=PositionState.LONG,
        left=GenericOperand.decision_close(),
        operator=ComparisonOperator.STRICTLY_LESS_THAN,
        right=GenericOperand.feature("minimum_45"),
        trigger_state=PositionState.CASH,
        non_trigger_state=PositionState.LONG,
        missing_data_action=MissingDataAction.PROTECTIVE_EXIT_TO_CASH,
    )
    definitions = (
        (
            "btc-usd-daily-sma-20-100-rejected-v1",
            BTC_USD_SMA_20_100_STRATEGY_FAMILY,
            "2e028203df214ac1484dfd567ba9cc85e1cbecd1ba193aeed76700272551da2f",
            _compatibility_definition(
                identity="btc-usd-daily-sma-20-100-rejected-v1",
                family=BTC_USD_SMA_20_100_STRATEGY_FAMILY,
                cadence=DecisionCadence.DAILY,
                mode=RuleMode.STATELESS_LONG_CASH,
                features=(mean20, mean100),
                rules=(sma_rule,),
                version=1,
            ),
        ),
        (
            "btc-usd-monthly-252-absolute-momentum-challenger-v1",
            BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
            "5a98e1444f88fe5be0e0cb3438af3dfbf3f4c35c855479d36b442e7c76e9e6d6",
            _compatibility_definition(
                identity="btc-usd-monthly-252-absolute-momentum-challenger-v1",
                family=BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
                cadence=DecisionCadence.CALENDAR_MONTHLY,
                mode=RuleMode.STATELESS_LONG_CASH,
                features=(return252,),
                rules=(momentum_rule,),
                version=2,
            ),
        ),
        (
            "btc-usd-monthly-200-observation-price-trend-v1",
            BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY,
            "c76c9d1690880803c293d25fd104eebbd9c2bd20d1cf9f60fcbe0a7e9f19dd4f",
            _compatibility_definition(
                identity="btc-usd-monthly-200-observation-price-trend-v1",
                family=BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY,
                cadence=DecisionCadence.CALENDAR_MONTHLY,
                mode=RuleMode.STATELESS_LONG_CASH,
                features=(mean200,),
                rules=(mean_rule,),
                version=3,
            ),
        ),
        (
            "btc-usd-monthly-stateful-90-45-breakout-v1",
            BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY,
            "5b81c588665ba08d3a13e206035d270b729dcda69555576b7fe9380e73291c98",
            _compatibility_definition(
                identity="btc-usd-monthly-stateful-90-45-breakout-v1",
                family=BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY,
                cadence=DecisionCadence.CALENDAR_MONTHLY,
                mode=RuleMode.STATEFUL_ENTRY_EXIT,
                features=(maximum90, minimum45),
                rules=(entry_rule, exit_rule),
                version=4,
            ),
        ),
    )
    result: list[GenericCompatibilityProjection] = []
    for identity, family, historical_semantics, definition in definitions:
        plan = compile_candidate_definition(definition)
        payload = (
            identity,
            family,
            historical_semantics,
            definition.canonical_hash,
            plan.canonical_hash,
            True,
            "ECONOMIC_EQUIVALENCE_ONLY_HISTORICAL_CANONICAL_HASH_NOT_REPLACED",
            "NONE",
        )
        result.append(
            GenericCompatibilityProjection(
                identity,
                family,
                historical_semantics,
                definition,
                plan,
                True,
                "ECONOMIC_EQUIVALENCE_ONLY_HISTORICAL_CANONICAL_HASH_NOT_REPLACED",
                content_digest(payload),
            )
        )
    return tuple(result)


def campaign_policy_v1_finding(
    *, campaign_question: str, campaign_canonical_hash: str, reviewed_at: datetime
) -> CampaignPolicyIntegrityFinding:
    return CampaignPolicyIntegrityFinding.create(
        finding_id="btc-usd-campaign-policy-v1-participation-integrity-review",
        reviewed_policy_id="CRYPTO_RESEARCH_CAMPAIGN_V1",
        reviewed_policy_hash=campaign_canonical_hash,
        campaign_question=campaign_question,
        actor="apex-research-integrity",
        reviewed_at=reviewed_at,
    )


def generic_workspace(
    *,
    definitions: tuple[GenericCandidateDefinition, ...] = (),
    plans: tuple[CompiledCandidatePlan, ...] = (),
) -> dict[str, object]:
    compatibility = retained_strategy_compatibility()
    return {
        "definition_schema_version": "GENERIC_CRYPTO_CANDIDATE_DEFINITION_V1",
        "compiler": "GENERIC_CRYPTO_CANDIDATE_COMPILER_V1",
        "executor": "GENERIC_CRYPTO_CANDIDATE_EXECUTOR_V1",
        "definitions": definitions,
        "compiled_plans": plans,
        "compatibility_projections": compatibility,
        "supported_primitives": {
            "features": (
                "ROLLING_MEAN",
                "ROLLING_MINIMUM",
                "ROLLING_MAXIMUM",
                "POINT_TO_POINT_RETURN",
            ),
            "operators": (
                "STRICTLY_GREATER_THAN",
                "STRICTLY_LESS_THAN",
                "GREATER_THAN_OR_EQUAL",
                "LESS_THAN_OR_EQUAL",
            ),
            "cadences": ("DAILY", "CALENDAR_MONTHLY"),
            "states": ("CASH", "LONG"),
            "rule_modes": ("STATELESS_LONG_CASH", "STATEFUL_ENTRY_EXIT"),
        },
        "unsupported": (
            "ARBITRARY_CODE",
            "ARBITRARY_FORMULAS",
            "DYNAMIC_IMPORTS",
            "PLUGINS",
            "SHORTING",
            "LEVERAGE",
            "DERIVATIVES",
            "PARTIAL_SIZING",
            "INTRADAY_EXECUTION",
            "LIMIT_ORDERS",
            "BROKER_SIMULATION",
        ),
        "historical_hash_claim": (
            "COMPATIBILITY PROJECTIONS ESTABLISH ECONOMIC REPRESENTABILITY; "
            "THEY DO NOT REPLACE RETAINED HISTORICAL HASHES"
        ),
        "holdout_accessed": False,
        "network_request_performed": False,
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "opportunity_authority": "NONE",
        "order_authority": "NONE",
        "execution_authority": "NONE",
        "financial_authority": "NONE",
        "authority": "NONE",
    }


def retained_candidate_view_models(repository: Any) -> tuple[dict[str, object], ...]:
    """Project retained numbered candidates through one read-only generic shape."""

    campaigns = repository.campaigns()
    if len(campaigns) != 1:
        return ()
    campaign = campaigns[0]
    candidates = repository.campaign_candidates(campaign.campaign_id)
    compatibility = {
        item.historical_strategy_id: item for item in retained_strategy_compatibility()
    }
    protocols = repository.candidate_protocols() + repository.breakout_protocols()
    runs = repository.candidate_runs() + repository.breakout_runs()
    outcomes = repository.candidate_outcomes() + repository.breakout_outcomes()
    dossiers = repository.candidate_dossiers() + repository.breakout_dossiers()
    nominations = repository.candidate_nominations() + repository.breakout_nominations()
    result: list[dict[str, object]] = []
    for candidate in candidates:
        projection = compatibility.get(candidate.strategy_specification_id)
        protocol = next(
            (item for item in protocols if item.candidate_id == candidate.candidate_id),
            None,
        )
        candidate_runs = tuple(item for item in runs if item.candidate_id == candidate.candidate_id)
        run_ids = {item.run_id for item in candidate_runs}
        candidate_outcomes = tuple(item for item in outcomes if item.run_id in run_ids)
        dossier = next(
            (item for item in dossiers if item.candidate_id == candidate.candidate_id),
            None,
        )
        nomination = next(
            (item for item in nominations if item.candidate_id == candidate.candidate_id),
            None,
        )
        result.append(
            {
                "candidate": candidate,
                "definition": projection.definition if projection is not None else None,
                "compiled_plan": projection.plan if projection is not None else None,
                "protocol": protocol,
                "runs": candidate_runs,
                "outcomes": candidate_outcomes,
                "dossier": dossier,
                "nomination": nomination,
                "compatibility_only": True,
                "historical_hashes_rewritten": False,
                "paper_eligible": False,
                "live_eligible": False,
                "financial_authority": "NONE",
                "authority": "NONE",
            }
        )
    return tuple(result)
