"""Application services for frozen crypto strategy evidence and replication."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from apex.application.crypto_research import (
    CryptoResearchConflict,
    CryptoResearchNotFound,
    CryptoResearchRepository,
    InMemoryCryptoResearchRepository,
    crypto_research_workspace,
)
from apex.domain.crypto_dossier import (
    CRYPTO_DISJOINT_REPLICATION_RESULT_V1,
    CRYPTO_DOSSIER_CONCLUSION_POLICY_V1,
    CRYPTO_EVIDENCE_DOSSIER_V1,
    NO_APPROVED_CHAMPION,
    ORIGINAL_CANONICAL_RUN_HASH,
    ORIGINAL_HYPOTHESIS_VERSION_ID,
    ORIGINAL_OUTCOME_HASH,
    ORIGINAL_OUTCOME_ID,
    ORIGINAL_RESULT_HASH,
    ORIGINAL_RUN_ID,
    ORIGINAL_SNAPSHOT_ID,
    ORIGINAL_SNAPSHOT_MANIFEST_HASH,
    REPLICATION_WARNINGS,
    CryptoDossierConclusion,
    CryptoDossierConclusionType,
    CryptoEvidenceDossier,
    CryptoReplicationOutcome,
    CryptoReplicationProtocol,
    CryptoReplicationRun,
    CryptoStrategySpecification,
    conservative_metric,
    run_passes_policy,
)
from apex.domain.crypto_research import (
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    CONSERVATIVE_FEE_BPS,
    CONSERVATIVE_SLIPPAGE_BPS,
    CRYPTO_RESEARCH_COST_MODEL_V1,
    CRYPTO_VALIDATION_POLICY_V1,
    RESEARCH_METRIC_QUANTUM,
    CryptoCaptureQuality,
    CryptoPerformanceMetrics,
    CryptoSignalDecision,
    PortfolioName,
    build_prior_day_signals,
    calculate_validation_metrics,
    expected_bucket_starts,
)
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
)
from apex.domain.values import content_digest


class CryptoDossierRepository(CryptoResearchRepository, Protocol):
    def strategy_specification(
        self, specification_id: str
    ) -> CryptoStrategySpecification | None: ...
    def strategy_by_semantics(self, semantics_hash: str) -> CryptoStrategySpecification | None: ...
    def save_strategy_specification(
        self, value: CryptoStrategySpecification
    ) -> CryptoStrategySpecification: ...
    def strategy_specifications(self) -> tuple[CryptoStrategySpecification, ...]: ...

    def replication_protocol(self, protocol_id: str) -> CryptoReplicationProtocol | None: ...
    def save_replication_protocol(
        self, value: CryptoReplicationProtocol
    ) -> CryptoReplicationProtocol: ...
    def replication_protocols(self) -> tuple[CryptoReplicationProtocol, ...]: ...

    def replication_run(self, run_id: str) -> CryptoReplicationRun | None: ...
    def save_replication_run(self, value: CryptoReplicationRun) -> CryptoReplicationRun: ...
    def replication_runs(self) -> tuple[CryptoReplicationRun, ...]: ...

    def replication_outcome(self, outcome_id: str) -> CryptoReplicationOutcome | None: ...
    def replication_outcome_for_run(self, run_id: str) -> CryptoReplicationOutcome | None: ...
    def save_replication_outcome(
        self, value: CryptoReplicationOutcome
    ) -> CryptoReplicationOutcome: ...
    def replication_outcomes(self) -> tuple[CryptoReplicationOutcome, ...]: ...

    def dossier(self, dossier_id: str) -> CryptoEvidenceDossier | None: ...
    def save_dossier(self, value: CryptoEvidenceDossier) -> CryptoEvidenceDossier: ...
    def dossiers(self) -> tuple[CryptoEvidenceDossier, ...]: ...

    def dossier_conclusion(self, conclusion_id: str) -> CryptoDossierConclusion | None: ...
    def dossier_conclusion_for_dossier(self, dossier_id: str) -> CryptoDossierConclusion | None: ...
    def save_dossier_conclusion(
        self, value: CryptoDossierConclusion
    ) -> CryptoDossierConclusion: ...
    def dossier_conclusions(self) -> tuple[CryptoDossierConclusion, ...]: ...


class InMemoryCryptoDossierRepository(InMemoryCryptoResearchRepository):
    def __init__(self) -> None:
        super().__init__()
        self._strategy_specs: dict[str, CryptoStrategySpecification] = {}
        self._replication_protocols: dict[str, CryptoReplicationProtocol] = {}
        self._replication_runs: dict[str, CryptoReplicationRun] = {}
        self._replication_outcomes: dict[str, CryptoReplicationOutcome] = {}
        self._dossiers: dict[str, CryptoEvidenceDossier] = {}
        self._dossier_conclusions: dict[str, CryptoDossierConclusion] = {}

    def strategy_specification(self, specification_id: str) -> CryptoStrategySpecification | None:
        return self._strategy_specs.get(specification_id)

    def strategy_by_semantics(self, semantics_hash: str) -> CryptoStrategySpecification | None:
        return next(
            (
                value
                for value in self._strategy_specs.values()
                if value.semantics_hash == semantics_hash
            ),
            None,
        )

    def save_strategy_specification(
        self, value: CryptoStrategySpecification
    ) -> CryptoStrategySpecification:
        prior = self.strategy_specification(value.specification_id)
        duplicate = self.strategy_by_semantics(value.semantics_hash)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_STRATEGY_SPECIFICATION_CONFLICT")
        if duplicate is not None and duplicate.specification_id != value.specification_id:
            raise CryptoResearchConflict("CRYPTO_DUPLICATE_STRATEGY_SPECIFICATION")
        self._strategy_specs[value.specification_id] = prior or value
        return prior or value

    def strategy_specifications(self) -> tuple[CryptoStrategySpecification, ...]:
        return tuple(
            sorted(self._strategy_specs.values(), key=lambda value: value.specification_id)
        )

    def replication_protocol(self, protocol_id: str) -> CryptoReplicationProtocol | None:
        return self._replication_protocols.get(protocol_id)

    def save_replication_protocol(
        self, value: CryptoReplicationProtocol
    ) -> CryptoReplicationProtocol:
        prior = self.replication_protocol(value.protocol_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_REPLICATION_PROTOCOL_CONFLICT")
        self._replication_protocols[value.protocol_id] = prior or value
        return prior or value

    def replication_protocols(self) -> tuple[CryptoReplicationProtocol, ...]:
        return tuple(
            sorted(self._replication_protocols.values(), key=lambda value: value.protocol_id)
        )

    def replication_run(self, run_id: str) -> CryptoReplicationRun | None:
        return self._replication_runs.get(run_id)

    def save_replication_run(self, value: CryptoReplicationRun) -> CryptoReplicationRun:
        prior = self.replication_run(value.run_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_REPLICATION_RUN_CONFLICT")
        self._replication_runs[value.run_id] = prior or value
        return prior or value

    def replication_runs(self) -> tuple[CryptoReplicationRun, ...]:
        return tuple(sorted(self._replication_runs.values(), key=lambda value: value.run_id))

    def replication_outcome(self, outcome_id: str) -> CryptoReplicationOutcome | None:
        return self._replication_outcomes.get(outcome_id)

    def replication_outcome_for_run(self, run_id: str) -> CryptoReplicationOutcome | None:
        return next(
            (value for value in self._replication_outcomes.values() if value.run_id == run_id),
            None,
        )

    def save_replication_outcome(self, value: CryptoReplicationOutcome) -> CryptoReplicationOutcome:
        prior = self.replication_outcome(value.outcome_id)
        run_prior = self.replication_outcome_for_run(value.run_id)
        if (prior is not None and prior.canonical_hash != value.canonical_hash) or (
            run_prior is not None and run_prior.canonical_hash != value.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_REPLICATION_OUTCOME_CONFLICT")
        self._replication_outcomes[value.outcome_id] = prior or value
        return prior or value

    def replication_outcomes(self) -> tuple[CryptoReplicationOutcome, ...]:
        return tuple(
            sorted(self._replication_outcomes.values(), key=lambda value: value.outcome_id)
        )

    def dossier(self, dossier_id: str) -> CryptoEvidenceDossier | None:
        return self._dossiers.get(dossier_id)

    def save_dossier(self, value: CryptoEvidenceDossier) -> CryptoEvidenceDossier:
        prior = self.dossier(value.dossier_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_DOSSIER_CONFLICT")
        self._dossiers[value.dossier_id] = prior or value
        return prior or value

    def dossiers(self) -> tuple[CryptoEvidenceDossier, ...]:
        return tuple(sorted(self._dossiers.values(), key=lambda value: value.dossier_id))

    def dossier_conclusion(self, conclusion_id: str) -> CryptoDossierConclusion | None:
        return self._dossier_conclusions.get(conclusion_id)

    def dossier_conclusion_for_dossier(self, dossier_id: str) -> CryptoDossierConclusion | None:
        return next(
            (
                value
                for value in self._dossier_conclusions.values()
                if value.dossier_id == dossier_id
            ),
            None,
        )

    def save_dossier_conclusion(self, value: CryptoDossierConclusion) -> CryptoDossierConclusion:
        prior = self.dossier_conclusion(value.conclusion_id)
        dossier_prior = self.dossier_conclusion_for_dossier(value.dossier_id)
        if (prior is not None and prior.canonical_hash != value.canonical_hash) or (
            dossier_prior is not None and dossier_prior.canonical_hash != value.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_DOSSIER_CONCLUSION_CONFLICT")
        self._dossier_conclusions[value.conclusion_id] = prior or value
        return prior or value

    def dossier_conclusions(self) -> tuple[CryptoDossierConclusion, ...]:
        return tuple(
            sorted(self._dossier_conclusions.values(), key=lambda value: value.conclusion_id)
        )


def verify_original_rejection(repository: CryptoResearchRepository) -> None:
    hypothesis = repository.hypothesis_version(ORIGINAL_HYPOTHESIS_VERSION_ID)
    snapshot = repository.snapshot(ORIGINAL_SNAPSHOT_ID)
    run = repository.run(ORIGINAL_RUN_ID)
    outcome = repository.outcome(ORIGINAL_OUTCOME_ID)
    if (
        hypothesis is None
        or snapshot is None
        or run is None
        or outcome is None
        or snapshot.manifest_hash != ORIGINAL_SNAPSHOT_MANIFEST_HASH
        or run.result_hash != ORIGINAL_RESULT_HASH
        or run.canonical_hash != ORIGINAL_CANONICAL_RUN_HASH
        or outcome.run_id != run.run_id
        or outcome.canonical_hash != ORIGINAL_OUTCOME_HASH
        or outcome.decision is not ExperimentOutcomeDecision.REJECTED
        or outcome.paper_eligible
        or outcome.live_eligible
    ):
        raise CryptoResearchConflict("CRYPTO_ORIGINAL_REJECTION_INTEGRITY_FAILED")


class RegisterFrozenCryptoStrategy:
    def __init__(self, repository: CryptoDossierRepository) -> None:
        self._repository = repository

    def execute(
        self, *, specification_id: str, created_at: datetime
    ) -> CryptoStrategySpecification:
        verify_original_rejection(self._repository)
        candidate = CryptoStrategySpecification.frozen(
            specification_id=specification_id, created_at=created_at
        )
        prior = self._repository.strategy_specification(specification_id)
        if prior is not None:
            if prior.semantics_hash != candidate.semantics_hash:
                raise CryptoResearchConflict("CRYPTO_STRATEGY_SPECIFICATION_CONFLICT")
            return prior
        duplicate = self._repository.strategy_by_semantics(candidate.semantics_hash)
        if duplicate is not None:
            raise CryptoResearchConflict(
                f"CRYPTO_DUPLICATE_STRATEGY_ALREADY_TESTED:{duplicate.specification_id}"
            )
        return self._repository.save_strategy_specification(candidate)


class RegisterCryptoReplicationProtocol:
    def __init__(self, repository: CryptoDossierRepository) -> None:
        self._repository = repository

    def execute(
        self, *, protocol_id: str, strategy_specification_id: str, created_at: datetime
    ) -> CryptoReplicationProtocol:
        verify_original_rejection(self._repository)
        strategy = self._repository.strategy_specification(strategy_specification_id)
        if strategy is None:
            raise CryptoResearchNotFound("CRYPTO_STRATEGY_SPECIFICATION_NOT_FOUND")
        candidate = CryptoReplicationProtocol.fixed(
            protocol_id=protocol_id, strategy=strategy, created_at=created_at
        )
        prior = self._repository.replication_protocol(protocol_id)
        if prior is not None:
            if prior.protocol_hash != candidate.protocol_hash:
                raise CryptoResearchConflict("CRYPTO_REPLICATION_PROTOCOL_CONFLICT")
            return prior
        return self._repository.save_replication_protocol(candidate)


def _replication_configuration(
    protocol: CryptoReplicationProtocol,
) -> tuple[tuple[str, object], ...]:
    return (
        ("product_id", COINBASE_PRODUCT),
        ("granularity", COINBASE_GRANULARITY),
        ("data_start", protocol.data_start),
        ("data_end", protocol.data_end),
        ("validation_start", protocol.validation_start),
        ("validation_end", protocol.validation_end),
        ("strategy_semantics_hash", protocol.strategy_semantics_hash),
        ("conservative_fee_bps_per_side", CONSERVATIVE_FEE_BPS),
        ("conservative_slippage_bps_per_side", CONSERVATIVE_SLIPPAGE_BPS),
        ("cost_model", CRYPTO_RESEARCH_COST_MODEL_V1),
        ("validation_policy", CRYPTO_VALIDATION_POLICY_V1),
    )


class ExecuteCryptoDisjointReplication:
    def __init__(self, repository: CryptoDossierRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        protocol_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CryptoReplicationRun:
        candidate = self._build(
            run_id=run_id,
            protocol_id=protocol_id,
            snapshot_id=snapshot_id,
            code_revision=code_revision,
            executed_at=executed_at,
        )
        prior = self._repository.replication_run(run_id)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_REPLICATION_RUN_CONFLICT")
            return prior
        return self._repository.save_replication_run(candidate)

    def replay(self, run_id: str) -> CryptoReplicationRun:
        prior = self._repository.replication_run(run_id)
        if prior is None:
            raise CryptoResearchNotFound("CRYPTO_REPLICATION_RUN_NOT_FOUND")
        replay = self._build(
            run_id=prior.run_id,
            protocol_id=prior.protocol_id,
            snapshot_id=prior.snapshot_id,
            code_revision=prior.code_revision,
            executed_at=prior.executed_at,
        )
        if replay.result_hash != prior.result_hash or replay.canonical_hash != prior.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_REPLICATION_REPLAY_MISMATCH")
        return prior

    def _build(
        self,
        *,
        run_id: str,
        protocol_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CryptoReplicationRun:
        verify_original_rejection(self._repository)
        protocol = self._repository.replication_protocol(protocol_id)
        snapshot = self._repository.snapshot(snapshot_id)
        if protocol is None:
            raise CryptoResearchNotFound("CRYPTO_REPLICATION_PROTOCOL_NOT_FOUND")
        strategy = self._repository.strategy_specification(protocol.strategy_specification_id)
        if strategy is None:
            raise CryptoResearchNotFound("CRYPTO_STRATEGY_SPECIFICATION_NOT_FOUND")
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_REPLICATION_SNAPSHOT_NOT_FOUND")
        expected_count = len(expected_bucket_starts(protocol.data_start, protocol.data_end))
        complete = (
            snapshot.product_id == COINBASE_PRODUCT
            and snapshot.start == protocol.data_start
            and snapshot.end == protocol.data_end
            and snapshot.earliest_bucket == protocol.data_start
            and snapshot.latest_bucket == protocol.data_end - timedelta(days=1)
            and snapshot.expected_buckets == expected_count
            and snapshot.accepted_buckets == expected_count
            and snapshot.quality is CryptoCaptureQuality.VALID
            and not snapshot.missing_bucket_starts
            and snapshot.rejected_records == 0
            and snapshot.duplicate_records == 0
            and strategy.semantics_hash == protocol.strategy_semantics_hash
        )
        configuration = _replication_configuration(protocol)
        configuration_hash = content_digest(configuration)
        signals: tuple[CryptoSignalDecision, ...] = ()
        metrics: tuple[CryptoPerformanceMetrics, ...] = ()
        failure_reason = None
        state = ExperimentState.COMPLETED
        if complete:
            candles = self._repository.candles(snapshot.capture_id)
            try:
                signals = build_prior_day_signals(
                    candles,
                    validation_start=protocol.validation_start,
                    validation_end=protocol.validation_end,
                )
                metrics = calculate_validation_metrics(
                    candles,
                    signals,
                    validation_start=protocol.validation_start,
                    validation_end=protocol.validation_end,
                )
            except ValueError as error:
                complete = False
                state = ExperimentState.FAILED
                failure_reason = str(error)
        else:
            failure_reason = "CRYPTO_REPLICATION_REQUIRED_EVIDENCE_INCOMPLETE"
        result_payload = (
            CRYPTO_DISJOINT_REPLICATION_RESULT_V1,
            protocol.protocol_hash,
            strategy.semantics_hash,
            snapshot.manifest_hash,
            configuration_hash,
            signals,
            metrics,
            REPLICATION_WARNINGS,
            complete,
            state,
            failure_reason,
        )
        result_hash = content_digest(result_payload)
        canonical = (
            run_id,
            protocol_id,
            snapshot_id,
            strategy.specification_id,
            code_revision,
            executed_at,
            result_hash,
        )
        return CryptoReplicationRun(
            run_id=run_id,
            protocol_id=protocol_id,
            strategy_specification_id=strategy.specification_id,
            snapshot_id=snapshot_id,
            strategy_semantics_hash=strategy.semantics_hash,
            configuration=configuration,
            configuration_hash=configuration_hash,
            code_revision=code_revision,
            deterministic_seed=0,
            executed_at=executed_at,
            state=state,
            complete=complete,
            reproducibility_status=ReproducibilityStatus.REPRODUCIBLE,
            evidence_class=snapshot.evidence_class,
            signals=signals,
            metrics=metrics,
            warnings=REPLICATION_WARNINGS,
            failure_reason=failure_reason,
            result_hash=result_hash,
            canonical_hash=content_digest(canonical),
        )


class RecordCryptoReplicationOutcome:
    def __init__(self, repository: CryptoDossierRepository) -> None:
        self._repository = repository

    def execute(
        self, *, outcome_id: str, run_id: str, actor: str, decided_at: datetime
    ) -> CryptoReplicationOutcome:
        run = self._repository.replication_run(run_id)
        if run is None:
            raise CryptoResearchNotFound("CRYPTO_REPLICATION_RUN_NOT_FOUND")
        prior = self._repository.replication_outcome_for_run(run_id)
        if prior is not None:
            if prior.outcome_id != outcome_id:
                raise CryptoResearchConflict("CRYPTO_REPLICATION_OUTCOME_ALREADY_RECORDED")
            return prior
        if (
            not run.complete
            or run.state is not ExperimentState.COMPLETED
            or run.evidence_class is not EvidenceClass.HISTORICAL_PUBLIC
        ):
            decision = ExperimentOutcomeDecision.DEFERRED
            reason = "Replication evidence was incomplete, non-genuine, or otherwise ineligible."
        elif run_passes_policy(run):
            decision = ExperimentOutcomeDecision.PROMOTION_CANDIDATE
            reason = (
                "The disjoint replication passed the unchanged original policy; "
                "this is mixed research evidence and grants no authority."
            )
        else:
            decision = ExperimentOutcomeDecision.REJECTED
            reason = "The complete disjoint replication also failed the unchanged original policy."
        payload = (outcome_id, run_id, decision, reason, decided_at, actor, run.result_hash)
        return self._repository.save_replication_outcome(
            CryptoReplicationOutcome(
                outcome_id=outcome_id,
                run_id=run_id,
                decision=decision,
                reason=reason,
                decided_at=decided_at,
                actor=actor,
                canonical_hash=content_digest(payload),
            )
        )


class MaterializeCryptoEvidenceDossier:
    def __init__(self, repository: CryptoDossierRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        dossier_id: str,
        replication_outcome_id: str,
        created_at: datetime,
    ) -> CryptoEvidenceDossier:
        verify_original_rejection(self._repository)
        original = self._repository.run(ORIGINAL_RUN_ID)
        original_outcome = self._repository.outcome(ORIGINAL_OUTCOME_ID)
        replication_outcome = self._repository.replication_outcome(replication_outcome_id)
        if original is None or original_outcome is None or replication_outcome is None:
            raise CryptoResearchNotFound("CRYPTO_DOSSIER_MEMBERSHIP_NOT_FOUND")
        replication = self._repository.replication_run(replication_outcome.run_id)
        if replication is None:
            raise CryptoResearchNotFound("CRYPTO_REPLICATION_RUN_NOT_FOUND")
        protocol = self._repository.replication_protocol(replication.protocol_id)
        strategy = self._repository.strategy_specification(replication.strategy_specification_id)
        if protocol is None or strategy is None:
            raise CryptoResearchNotFound("CRYPTO_DOSSIER_GOVERNANCE_NOT_FOUND")
        original_trend = conservative_metric(original.metrics, PortfolioName.TREND_RULE)
        replication_trend = (
            conservative_metric(replication.metrics, PortfolioName.TREND_RULE)
            if replication.metrics
            else None
        )
        net_returns = [original_trend.net_return]
        if replication_trend is not None:
            net_returns.append(replication_trend.net_return)
        net_returns.sort()
        median = (
            ((net_returns[0] + net_returns[1]) / Decimal(2)).quantize(RESEARCH_METRIC_QUANTUM)
            if len(net_returns) == 2
            else net_returns[0]
        )
        passing = int(original_outcome.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE)
        passing += int(
            replication_outcome.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE
        )
        failing = int(original_outcome.decision is ExperimentOutcomeDecision.REJECTED)
        failing += int(replication_outcome.decision is ExperimentOutcomeDecision.REJECTED)
        complete_windows = int(original.complete) + int(replication.complete)
        consistency = (
            "CONSISTENT_REJECTION"
            if failing == 2
            else "MIXED_POLICY_RESULTS"
            if passing == 1 and failing == 1
            else "INCONCLUSIVE_EVIDENCE"
        )
        membership = (
            strategy.specification_id,
            strategy.hypothesis_version_id,
            ORIGINAL_SNAPSHOT_ID,
            ORIGINAL_RUN_ID,
            ORIGINAL_RESULT_HASH,
            ORIGINAL_OUTCOME_ID,
            protocol.protocol_id,
            replication.snapshot_id,
            replication.run_id,
            replication.result_hash,
            replication_outcome.outcome_id,
            strategy.semantics_hash,
        )
        membership_hash = content_digest(membership)
        created = created_at
        canonical = (
            CRYPTO_EVIDENCE_DOSSIER_V1,
            dossier_id,
            membership_hash,
            complete_windows,
            passing,
            failing,
            median,
            max(
                original_trend.maximum_drawdown,
                replication_trend.maximum_drawdown
                if replication_trend is not None
                else original_trend.maximum_drawdown,
            ),
            consistency,
            created,
        )
        candidate = CryptoEvidenceDossier(
            dossier_id=dossier_id,
            strategy_specification_id=strategy.specification_id,
            hypothesis_version_id=strategy.hypothesis_version_id,
            original_snapshot_id=ORIGINAL_SNAPSHOT_ID,
            original_run_id=ORIGINAL_RUN_ID,
            original_result_hash=ORIGINAL_RESULT_HASH,
            original_outcome_id=ORIGINAL_OUTCOME_ID,
            replication_protocol_id=protocol.protocol_id,
            replication_snapshot_id=replication.snapshot_id,
            replication_run_id=replication.run_id,
            replication_result_hash=replication.result_hash,
            replication_outcome_id=replication_outcome.outcome_id,
            shared_semantics_hash=strategy.semantics_hash,
            complete_windows=complete_windows,
            passing_windows=passing,
            failing_windows=failing,
            median_conservative_net_return=median,
            worst_maximum_drawdown=max(
                original_trend.maximum_drawdown,
                replication_trend.maximum_drawdown
                if replication_trend is not None
                else original_trend.maximum_drawdown,
            ),
            consistency_status=consistency,
            warnings=tuple(dict.fromkeys((*original.warnings, *replication.warnings))),
            created_at=created,
            membership_hash=membership_hash,
            canonical_hash=content_digest(canonical),
        )
        prior = self._repository.dossier(dossier_id)
        if prior is not None:
            if prior.membership_hash != candidate.membership_hash:
                raise CryptoResearchConflict("CRYPTO_DOSSIER_CONFLICT")
            return prior
        return self._repository.save_dossier(candidate)


class RecordCryptoDossierConclusion:
    def __init__(self, repository: CryptoDossierRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        conclusion_id: str,
        dossier_id: str,
        actor: str,
        decided_at: datetime,
    ) -> CryptoDossierConclusion:
        dossier = self._repository.dossier(dossier_id)
        if dossier is None:
            raise CryptoResearchNotFound("CRYPTO_DOSSIER_NOT_FOUND")
        replication_outcome = self._repository.replication_outcome(dossier.replication_outcome_id)
        replication = self._repository.replication_run(dossier.replication_run_id)
        if replication_outcome is None or replication is None:
            raise CryptoResearchNotFound("CRYPTO_DOSSIER_MEMBERSHIP_NOT_FOUND")
        if (
            not replication.complete
            or replication.reproducibility_status is not ReproducibilityStatus.REPRODUCIBLE
            or replication_outcome.decision is ExperimentOutcomeDecision.DEFERRED
        ):
            conclusion = CryptoDossierConclusionType.INCONCLUSIVE
            reason = (
                "Replication quality, completeness, evidence class, or "
                "reproducibility prevented a decision."
            )
        elif replication_outcome.decision is ExperimentOutcomeDecision.REJECTED:
            conclusion = CryptoDossierConclusionType.REJECTION_CONFIRMED
            reason = (
                "Both complete genuine windows failed the unchanged predeclared success policy."
            )
        else:
            conclusion = CryptoDossierConclusionType.MIXED_EVIDENCE
            reason = (
                "The original remains rejected while the disjoint replication "
                "passed the same policy."
            )
        payload = (
            conclusion_id,
            dossier_id,
            conclusion,
            reason,
            CRYPTO_DOSSIER_CONCLUSION_POLICY_V1,
            decided_at,
            actor,
            dossier.canonical_hash,
        )
        candidate = CryptoDossierConclusion(
            conclusion_id=conclusion_id,
            dossier_id=dossier_id,
            conclusion=conclusion,
            reason=reason,
            policy_version=CRYPTO_DOSSIER_CONCLUSION_POLICY_V1,
            decided_at=decided_at,
            actor=actor,
            canonical_hash=content_digest(payload),
        )
        prior = self._repository.dossier_conclusion_for_dossier(dossier_id)
        if prior is not None:
            if prior.conclusion_id != conclusion_id:
                raise CryptoResearchConflict("CRYPTO_DOSSIER_CONCLUSION_ALREADY_RECORDED")
            return prior
        return self._repository.save_dossier_conclusion(candidate)


def crypto_dossier_workspace(repository: CryptoDossierRepository) -> dict[str, object]:
    base = crypto_research_workspace(repository)
    base.update(
        {
            "strategy_specifications": repository.strategy_specifications(),
            "replication_protocols": repository.replication_protocols(),
            "replication_runs": repository.replication_runs(),
            "replication_outcomes": repository.replication_outcomes(),
            "dossiers": repository.dossiers(),
            "dossier_conclusions": repository.dossier_conclusions(),
            "approved_champion_status": NO_APPROVED_CHAMPION,
            "negative_knowledge_count": len(repository.strategy_specifications()),
            "research_state": "REJECTED_RESEARCH",
            "capital_authority": "NONE",
            "opportunity_authority": "NONE",
            "order_authority": "NONE",
        }
    )
    return base
