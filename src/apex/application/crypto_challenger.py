"""Application services for the retained-evidence monthly BTC challenger."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.application.crypto_dossier import (
    CryptoDossierRepository,
    InMemoryCryptoDossierRepository,
    verify_original_rejection,
)
from apex.application.crypto_research import CryptoResearchConflict, CryptoResearchNotFound
from apex.domain.crypto_challenger import (
    AUTHORITY_WARNINGS,
    CRYPTO_MONTHLY_CHALLENGER_CONCLUSION_V1,
    CRYPTO_MONTHLY_CHALLENGER_DOSSIER_V1,
    CRYPTO_MONTHLY_CHALLENGER_RESULT_V1,
    EARLIER_PRIOR_RUN_ID,
    FIXED_WINDOWS,
    LATER_PRIOR_RUN_ID,
    LEADING_RESEARCH_CHALLENGER_REQUIRES_FORWARD_VALIDATION,
    NO_LEADING_CHALLENGER,
    ChallengerConclusionType,
    ChallengerPortfolio,
    CryptoChallengerConclusion,
    CryptoChallengerDossier,
    CryptoChallengerOutcome,
    CryptoChallengerProtocol,
    CryptoChallengerRun,
    MonthlyMomentumHypothesisSpecification,
    RetainedPriorComparison,
    StrategySemanticComparison,
    build_monthly_momentum_decisions,
    calculate_challenger_metrics,
    challenger_window_passes,
    conservative_challenger_metric,
)
from apex.domain.crypto_dossier import (
    BTC_USD_SMA_20_100_STRATEGY_FAMILY,
    NO_APPROVED_CHAMPION,
    CryptoReplicationRun,
    CryptoStrategyLifecycle,
    CryptoStrategySpecification,
    conservative_metric,
)
from apex.domain.crypto_research import (
    CryptoCaptureQuality,
    CryptoExperimentRun,
    CryptoHypothesisSpecification,
    PortfolioName,
    expected_bucket_starts,
)
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    HypothesisStatus,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import content_digest


class CryptoChallengerRepository(CryptoDossierRepository, Protocol):
    def challenger_hypothesis(
        self, hypothesis_version_id: str
    ) -> MonthlyMomentumHypothesisSpecification | None: ...
    def save_challenger_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: MonthlyMomentumHypothesisSpecification,
    ) -> MonthlyMomentumHypothesisSpecification: ...
    def challenger_hypotheses(self) -> tuple[MonthlyMomentumHypothesisSpecification, ...]: ...

    def challenger_protocol(self, protocol_id: str) -> CryptoChallengerProtocol | None: ...
    def save_challenger_protocol(
        self, value: CryptoChallengerProtocol
    ) -> CryptoChallengerProtocol: ...
    def challenger_protocols(self) -> tuple[CryptoChallengerProtocol, ...]: ...

    def challenger_run(self, run_id: str) -> CryptoChallengerRun | None: ...
    def save_challenger_run(self, value: CryptoChallengerRun) -> CryptoChallengerRun: ...
    def challenger_runs(self) -> tuple[CryptoChallengerRun, ...]: ...

    def challenger_outcome(self, outcome_id: str) -> CryptoChallengerOutcome | None: ...
    def challenger_outcome_for_run(self, run_id: str) -> CryptoChallengerOutcome | None: ...
    def save_challenger_outcome(
        self, value: CryptoChallengerOutcome
    ) -> CryptoChallengerOutcome: ...
    def challenger_outcomes(self) -> tuple[CryptoChallengerOutcome, ...]: ...

    def challenger_dossier(self, dossier_id: str) -> CryptoChallengerDossier | None: ...
    def save_challenger_dossier(
        self, value: CryptoChallengerDossier
    ) -> CryptoChallengerDossier: ...
    def challenger_dossiers(self) -> tuple[CryptoChallengerDossier, ...]: ...

    def challenger_conclusion(self, conclusion_id: str) -> CryptoChallengerConclusion | None: ...
    def challenger_conclusion_for_dossier(
        self, dossier_id: str
    ) -> CryptoChallengerConclusion | None: ...
    def save_challenger_conclusion(
        self, value: CryptoChallengerConclusion
    ) -> CryptoChallengerConclusion: ...
    def challenger_conclusions(self) -> tuple[CryptoChallengerConclusion, ...]: ...


class InMemoryCryptoChallengerRepository(InMemoryCryptoDossierRepository):
    def __init__(self) -> None:
        super().__init__()
        self._challenger_hypotheses: dict[str, MonthlyMomentumHypothesisSpecification] = {}
        self._challenger_protocols: dict[str, CryptoChallengerProtocol] = {}
        self._challenger_runs: dict[str, CryptoChallengerRun] = {}
        self._challenger_outcomes: dict[str, CryptoChallengerOutcome] = {}
        self._challenger_dossiers: dict[str, CryptoChallengerDossier] = {}
        self._challenger_conclusions: dict[str, CryptoChallengerConclusion] = {}

    def hypotheses(
        self,
    ) -> tuple[tuple[ResearchHypothesisVersion, CryptoHypothesisSpecification], ...]:
        return tuple(
            (item, self._specifications[item.hypothesis_version_id])
            for item in sorted(
                self._hypotheses.values(),
                key=lambda value: (value.hypothesis_id, value.revision),
            )
            if item.hypothesis_version_id in self._specifications
        )

    def challenger_hypothesis(
        self, hypothesis_version_id: str
    ) -> MonthlyMomentumHypothesisSpecification | None:
        return self._challenger_hypotheses.get(hypothesis_version_id)

    def save_challenger_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: MonthlyMomentumHypothesisSpecification,
    ) -> MonthlyMomentumHypothesisSpecification:
        prior_hypothesis = self._hypotheses.get(hypothesis.hypothesis_version_id)
        prior = self.challenger_hypothesis(hypothesis.hypothesis_version_id)
        if (
            prior_hypothesis is not None
            and prior_hypothesis.canonical_hash != hypothesis.canonical_hash
        ) or (prior is not None and prior.canonical_hash != specification.canonical_hash):
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_HYPOTHESIS_CONFLICT")
        self._hypotheses[hypothesis.hypothesis_version_id] = prior_hypothesis or hypothesis
        self._challenger_hypotheses[hypothesis.hypothesis_version_id] = prior or specification
        return prior or specification

    def challenger_hypotheses(self) -> tuple[MonthlyMomentumHypothesisSpecification, ...]:
        return tuple(
            sorted(
                self._challenger_hypotheses.values(),
                key=lambda value: value.hypothesis_version_id,
            )
        )

    def challenger_protocol(self, protocol_id: str) -> CryptoChallengerProtocol | None:
        return self._challenger_protocols.get(protocol_id)

    def save_challenger_protocol(self, value: CryptoChallengerProtocol) -> CryptoChallengerProtocol:
        prior = self.challenger_protocol(value.protocol_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_PROTOCOL_CONFLICT")
        self._challenger_protocols[value.protocol_id] = prior or value
        return prior or value

    def challenger_protocols(self) -> tuple[CryptoChallengerProtocol, ...]:
        return tuple(
            sorted(self._challenger_protocols.values(), key=lambda value: value.protocol_id)
        )

    def challenger_run(self, run_id: str) -> CryptoChallengerRun | None:
        return self._challenger_runs.get(run_id)

    def save_challenger_run(self, value: CryptoChallengerRun) -> CryptoChallengerRun:
        prior = self.challenger_run(value.run_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_RUN_CONFLICT")
        duplicate = next(
            (
                item
                for item in self._challenger_runs.values()
                if item.protocol_id == value.protocol_id
                and item.snapshot_id == value.snapshot_id
                and item.run_id != value.run_id
            ),
            None,
        )
        if duplicate is not None:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_WINDOW_ALREADY_EVALUATED")
        self._challenger_runs[value.run_id] = prior or value
        return prior or value

    def challenger_runs(self) -> tuple[CryptoChallengerRun, ...]:
        return tuple(sorted(self._challenger_runs.values(), key=lambda value: value.run_id))

    def challenger_outcome(self, outcome_id: str) -> CryptoChallengerOutcome | None:
        return self._challenger_outcomes.get(outcome_id)

    def challenger_outcome_for_run(self, run_id: str) -> CryptoChallengerOutcome | None:
        return next(
            (value for value in self._challenger_outcomes.values() if value.run_id == run_id),
            None,
        )

    def save_challenger_outcome(self, value: CryptoChallengerOutcome) -> CryptoChallengerOutcome:
        prior = self.challenger_outcome(value.outcome_id)
        run_prior = self.challenger_outcome_for_run(value.run_id)
        if (prior is not None and prior.canonical_hash != value.canonical_hash) or (
            run_prior is not None and run_prior.canonical_hash != value.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_OUTCOME_CONFLICT")
        self._challenger_outcomes[value.outcome_id] = prior or value
        return prior or value

    def challenger_outcomes(self) -> tuple[CryptoChallengerOutcome, ...]:
        return tuple(sorted(self._challenger_outcomes.values(), key=lambda value: value.outcome_id))

    def challenger_dossier(self, dossier_id: str) -> CryptoChallengerDossier | None:
        return self._challenger_dossiers.get(dossier_id)

    def save_challenger_dossier(self, value: CryptoChallengerDossier) -> CryptoChallengerDossier:
        prior = self.challenger_dossier(value.dossier_id)
        duplicate = next(
            (
                item
                for item in self._challenger_dossiers.values()
                if item.membership_hash == value.membership_hash
            ),
            None,
        )
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_DOSSIER_CONFLICT")
        if duplicate is not None and duplicate.dossier_id != value.dossier_id:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_DOSSIER_DUPLICATE")
        self._challenger_dossiers[value.dossier_id] = prior or value
        return prior or value

    def challenger_dossiers(self) -> tuple[CryptoChallengerDossier, ...]:
        return tuple(sorted(self._challenger_dossiers.values(), key=lambda value: value.dossier_id))

    def challenger_conclusion(self, conclusion_id: str) -> CryptoChallengerConclusion | None:
        return self._challenger_conclusions.get(conclusion_id)

    def challenger_conclusion_for_dossier(
        self, dossier_id: str
    ) -> CryptoChallengerConclusion | None:
        return next(
            (
                value
                for value in self._challenger_conclusions.values()
                if value.dossier_id == dossier_id
            ),
            None,
        )

    def save_challenger_conclusion(
        self, value: CryptoChallengerConclusion
    ) -> CryptoChallengerConclusion:
        prior = self.challenger_conclusion(value.conclusion_id)
        dossier_prior = self.challenger_conclusion_for_dossier(value.dossier_id)
        if (prior is not None and prior.canonical_hash != value.canonical_hash) or (
            dossier_prior is not None and dossier_prior.canonical_hash != value.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_CONCLUSION_CONFLICT")
        self._challenger_conclusions[value.conclusion_id] = prior or value
        return prior or value

    def challenger_conclusions(self) -> tuple[CryptoChallengerConclusion, ...]:
        return tuple(
            sorted(self._challenger_conclusions.values(), key=lambda value: value.conclusion_id)
        )


class RegisterMonthlyMomentumHypothesis:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        hypothesis_id: str,
        actor: str,
        created_at: datetime,
    ) -> tuple[ResearchHypothesisVersion, MonthlyMomentumHypothesisSpecification]:
        prior_revision = self._repository.hypothesis_revision(hypothesis_id, 1)
        if prior_revision is not None:
            specification = self._repository.challenger_hypothesis(
                prior_revision.hypothesis_version_id
            )
            if specification is None:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_HYPOTHESIS_CONFLICT")
            return prior_revision, specification
        hypothesis = ResearchHypothesisVersion.create(
            hypothesis_id=hypothesis_id,
            revision=1,
            question=(
                "Will the predeclared monthly BTC-USD 252-observation absolute-momentum "
                "long-or-cash rule produce non-negative conservative net return and "
                "lower drawdown than buy-and-hold in each of two disjoint windows?"
            ),
            motivation=(
                "Test a materially different low-frequency absolute trend threshold "
                "against cash, buy-and-hold, and the retained rejected 20/100 result "
                "using only two immutable genuine historical snapshots. No authority."
            ),
            actor=actor,
            market_scope=(
                "BTC-USD",
                "COINBASE_RETAINED_DAILY_HISTORICAL_PUBLIC",
                "VALIDATION_2023-07-01/2024-07-01",
                "VALIDATION_2025-07-01/2026-07-01",
                "MONTHLY_252_OBSERVATION_ABSOLUTE_MOMENTUM",
                "NO_PAPER_LIVE_CAPITAL_OPPORTUNITY_OR_ORDER_AUTHORITY",
            ),
            created_at=created_at,
            status=HypothesisStatus.REGISTERED,
        )
        specification = MonthlyMomentumHypothesisSpecification.fixed(hypothesis)
        self._repository.save_challenger_hypothesis(hypothesis, specification)
        return hypothesis, specification


class RegisterMonthlyMomentumStrategy:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        specification_id: str,
        hypothesis_version_id: str,
        parent_specification_id: str,
        created_at: datetime,
    ) -> tuple[CryptoStrategySpecification, StrategySemanticComparison]:
        verify_original_rejection(self._repository)
        hypothesis = self._repository.challenger_hypothesis(hypothesis_version_id)
        parent = self._repository.strategy_specification(parent_specification_id)
        if hypothesis is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_HYPOTHESIS_NOT_FOUND")
        if parent is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_PARENT_STRATEGY_NOT_FOUND")
        if (
            parent.strategy_family != BTC_USD_SMA_20_100_STRATEGY_FAMILY
            or parent.lifecycle_state is not CryptoStrategyLifecycle.REJECTED_RESEARCH
        ):
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_PARENT_INVALID")
        candidate = CryptoStrategySpecification.monthly_momentum_challenger(
            specification_id=specification_id,
            hypothesis_version_id=hypothesis_version_id,
            parent=parent,
            created_at=created_at,
        )
        comparison = StrategySemanticComparison.create(prior=parent, challenger=candidate)
        prior = self._repository.strategy_specification(specification_id)
        if prior is not None:
            if prior.semantics_hash != candidate.semantics_hash:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_STRATEGY_CONFLICT")
            return prior, StrategySemanticComparison.create(prior=parent, challenger=prior)
        duplicate = self._repository.strategy_by_semantics(candidate.semantics_hash)
        if duplicate is not None:
            raise CryptoResearchConflict(
                f"CRYPTO_DUPLICATE_STRATEGY_ALREADY_TESTED:{duplicate.specification_id}"
            )
        self._repository.save_strategy_specification(candidate)
        return candidate, comparison


class RegisterCryptoChallengerProtocol:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        protocol_id: str,
        hypothesis_version_id: str,
        strategy_specification_id: str,
        code_revision: str,
        created_at: datetime,
    ) -> CryptoChallengerProtocol:
        hypothesis = self._repository.challenger_hypothesis(hypothesis_version_id)
        strategy = self._repository.strategy_specification(strategy_specification_id)
        if hypothesis is None or strategy is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_PROTOCOL_INPUT_NOT_FOUND")
        for window in FIXED_WINDOWS:
            snapshot = self._repository.snapshot(window.snapshot_id)
            if (
                snapshot is None
                or snapshot.evidence_class is not EvidenceClass.HISTORICAL_PUBLIC
                or snapshot.quality is not CryptoCaptureQuality.VALID
            ):
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_RETAINED_SNAPSHOT_INVALID")
        candidate = CryptoChallengerProtocol.fixed(
            protocol_id=protocol_id,
            hypothesis=hypothesis,
            strategy=strategy,
            code_revision=code_revision,
            created_at=created_at,
        )
        prior = self._repository.challenger_protocol(protocol_id)
        if prior is not None:
            if prior.protocol_hash != candidate.protocol_hash:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_PROTOCOL_CONFLICT")
            return prior
        return self._repository.save_challenger_protocol(candidate)


class ExecuteCryptoChallengerWindow:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        protocol_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CryptoChallengerRun:
        candidate = self._build(
            run_id=run_id,
            protocol_id=protocol_id,
            snapshot_id=snapshot_id,
            code_revision=code_revision,
            executed_at=executed_at,
        )
        prior = self._repository.challenger_run(run_id)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_RUN_CONFLICT")
            return prior
        return self._repository.save_challenger_run(candidate)

    def replay(self, run_id: str) -> CryptoChallengerRun:
        prior = self._repository.challenger_run(run_id)
        if prior is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_RUN_NOT_FOUND")
        replay = self._build(
            run_id=prior.run_id,
            protocol_id=prior.protocol_id,
            snapshot_id=prior.snapshot_id,
            code_revision=prior.code_revision,
            executed_at=prior.executed_at,
        )
        if replay.result_hash != prior.result_hash or replay.canonical_hash != prior.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_REPLAY_MISMATCH")
        return prior

    def _prior_run(self, run_id: str) -> CryptoReplicationRun | CryptoExperimentRun | None:
        if run_id == EARLIER_PRIOR_RUN_ID:
            return self._repository.replication_run(run_id)
        if run_id == LATER_PRIOR_RUN_ID:
            return self._repository.run(run_id)
        return None

    def _build(
        self,
        *,
        run_id: str,
        protocol_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CryptoChallengerRun:
        protocol = self._repository.challenger_protocol(protocol_id)
        snapshot = self._repository.snapshot(snapshot_id)
        if protocol is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_PROTOCOL_NOT_FOUND")
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_SNAPSHOT_NOT_FOUND")
        if code_revision != protocol.code_revision:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_CODE_REVISION_MISMATCH")
        try:
            window = next(value for value in protocol.windows if value.snapshot_id == snapshot_id)
        except StopIteration as error:
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_SNAPSHOT_NOT_IN_PROTOCOL") from error
        prior_run = self._prior_run(window.prior_run_id)
        if prior_run is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_PRIOR_RUN_NOT_FOUND")
        expected_count = len(expected_bucket_starts(snapshot.start, snapshot.end))
        complete = (
            snapshot.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
            and snapshot.quality is CryptoCaptureQuality.VALID
            and snapshot.accepted_buckets == expected_count
            and snapshot.expected_buckets == expected_count
            and not snapshot.missing_bucket_starts
            and snapshot.rejected_records == 0
            and snapshot.duplicate_records == 0
            and prior_run.snapshot_id == snapshot_id
        )
        candles = self._repository.candles(snapshot.capture_id)
        decisions = build_monthly_momentum_decisions(
            candles,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
        )
        metrics = calculate_challenger_metrics(
            candles=candles,
            decisions=decisions,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
        )
        challenger = conservative_challenger_metric(metrics, ChallengerPortfolio.MONTHLY_MOMENTUM)
        prior_metric = conservative_metric(prior_run.metrics, PortfolioName.TREND_RULE)
        comparison = RetainedPriorComparison.create(
            prior_run_id=prior_run.run_id,
            prior_result_hash=prior_run.result_hash,
            prior_canonical_run_hash=prior_run.canonical_hash,
            prior_metric=prior_metric,
            challenger_metric=challenger,
        )
        if any(not decision.eligible for decision in decisions):
            complete = False
        state = ExperimentState.COMPLETED if complete else ExperimentState.FAILED
        reproducibility = (
            ReproducibilityStatus.REPRODUCIBLE
            if complete
            else ReproducibilityStatus.NOT_REPRODUCIBLE
        )
        failure_reason = None if complete else "CRYPTO_CHALLENGER_REQUIRED_EVIDENCE_INCOMPLETE"
        configuration = (
            protocol.protocol_hash,
            protocol.strategy_semantics_hash,
            snapshot.manifest_hash,
            window.validation_start,
            window.validation_end,
            prior_run.run_id,
            prior_run.result_hash,
        )
        configuration_hash = content_digest(configuration)
        result_payload = (
            CRYPTO_MONTHLY_CHALLENGER_RESULT_V1,
            configuration_hash,
            decisions,
            metrics,
            comparison,
            AUTHORITY_WARNINGS,
            complete,
            state,
            reproducibility,
            failure_reason,
        )
        result_hash = content_digest(result_payload)
        canonical = (
            run_id,
            protocol_id,
            snapshot_id,
            protocol.hypothesis_version_id,
            protocol.strategy_specification_id,
            code_revision,
            executed_at,
            result_hash,
        )
        return CryptoChallengerRun(
            run_id,
            protocol_id,
            protocol.hypothesis_version_id,
            protocol.strategy_specification_id,
            protocol.strategy_semantics_hash,
            snapshot_id,
            snapshot.manifest_hash,
            window.validation_start,
            window.validation_end,
            code_revision,
            0,
            executed_at,
            state,
            complete,
            reproducibility,
            snapshot.evidence_class,
            decisions,
            metrics,
            comparison,
            AUTHORITY_WARNINGS,
            failure_reason,
            configuration_hash,
            result_hash,
            content_digest(canonical),
        )


class RecordCryptoChallengerOutcome:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self, *, outcome_id: str, run_id: str, actor: str, decided_at: datetime
    ) -> CryptoChallengerOutcome:
        run = self._repository.challenger_run(run_id)
        if run is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_RUN_NOT_FOUND")
        prior = self._repository.challenger_outcome_for_run(run_id)
        if prior is not None:
            if prior.outcome_id != outcome_id:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_OUTCOME_ALREADY_RECORDED")
            return prior
        if not run.complete or run.reproducibility_status is not ReproducibilityStatus.REPRODUCIBLE:
            decision = ExperimentOutcomeDecision.DEFERRED
            reason = "Evidence incompleteness or reproducibility prevented a valid decision."
        elif challenger_window_passes(run):
            decision = ExperimentOutcomeDecision.PROMOTION_CANDIDATE
            reason = (
                "The window passed every predeclared retrospective research criterion; "
                "this grants no PAPER, LIVE, capital, opportunity, or order authority."
            )
        else:
            decision = ExperimentOutcomeDecision.REJECTED
            reason = "The complete window failed at least one predeclared success criterion."
        payload = (outcome_id, run_id, decision, reason, decided_at, actor, run.result_hash)
        return self._repository.save_challenger_outcome(
            CryptoChallengerOutcome(
                outcome_id,
                run_id,
                decision,
                reason,
                decided_at,
                actor,
                content_digest(payload),
            )
        )


class MaterializeCryptoChallengerDossier:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        dossier_id: str,
        protocol_id: str,
        earlier_outcome_id: str,
        later_outcome_id: str,
        created_at: datetime,
    ) -> CryptoChallengerDossier:
        protocol = self._repository.challenger_protocol(protocol_id)
        strategy = (
            self._repository.strategy_specification(protocol.strategy_specification_id)
            if protocol is not None
            else None
        )
        if protocol is None or strategy is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_DOSSIER_GOVERNANCE_NOT_FOUND")
        parent = self._repository.strategy_specification(strategy.parent_specification_id or "")
        earlier_outcome = self._repository.challenger_outcome(earlier_outcome_id)
        later_outcome = self._repository.challenger_outcome(later_outcome_id)
        if parent is None or earlier_outcome is None or later_outcome is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_DOSSIER_MEMBERSHIP_NOT_FOUND")
        runs = tuple(
            self._repository.challenger_run(outcome.run_id)
            for outcome in (earlier_outcome, later_outcome)
        )
        if any(run is None for run in runs):
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_RUN_NOT_FOUND")
        concrete = tuple(run for run in runs if run is not None)
        by_snapshot = {run.snapshot_id: run for run in concrete}
        if tuple(by_snapshot) != tuple(window.snapshot_id for window in FIXED_WINDOWS):
            concrete = tuple(by_snapshot[window.snapshot_id] for window in FIXED_WINDOWS)
        if (
            len(concrete) != 2
            or any(run.protocol_id != protocol_id for run in concrete)
            or len({run.strategy_semantics_hash for run in concrete}) != 1
        ):
            raise CryptoResearchConflict("CRYPTO_CHALLENGER_DOSSIER_WINDOW_MISMATCH")
        comparison = StrategySemanticComparison.create(prior=parent, challenger=strategy)
        outcomes = (earlier_outcome, later_outcome)
        metrics = tuple(
            conservative_challenger_metric(run.metrics, ChallengerPortfolio.MONTHLY_MOMENTUM)
            for run in concrete
        )
        baselines = tuple(
            conservative_challenger_metric(run.metrics, ChallengerPortfolio.BUY_AND_HOLD)
            for run in concrete
        )
        complete = sum(run.complete for run in concrete)
        passing = sum(
            outcome.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE
            for outcome in outcomes
        )
        failing = sum(
            outcome.decision is ExperimentOutcomeDecision.REJECTED for outcome in outcomes
        )
        inconclusive = 2 - passing - failing
        returns = sorted(metric.net_return for metric in metrics)
        turnovers = sorted(metric.turnover for metric in metrics)
        compounded = ((Decimal(1) + returns[0]) * (Decimal(1) + returns[1]) - Decimal(1)).quantize(
            Decimal("0.00000001")
        )
        median = ((returns[0] + returns[1]) / Decimal(2)).quantize(Decimal("0.00000001"))
        median_turnover = ((turnovers[0] + turnovers[1]) / Decimal(2)).quantize(
            Decimal("0.00000001")
        )
        percentage_cash = (
            Decimal(sum(metric.net_return > 0 for metric in metrics)) / Decimal(2) * Decimal(100)
        ).quantize(Decimal("0.00000001"))
        percentage_drawdown = (
            Decimal(
                sum(
                    metric.maximum_drawdown < baseline.maximum_drawdown
                    for metric, baseline in zip(metrics, baselines, strict=True)
                )
            )
            / Decimal(2)
            * Decimal(100)
        ).quantize(Decimal("0.00000001"))
        consistency = (
            "CONSISTENT_PASS"
            if passing == 2
            else "CONSISTENT_REJECTION"
            if failing == 2
            else "MIXED_POLICY_RESULTS"
            if passing == 1 and failing == 1
            else "INCONCLUSIVE_EVIDENCE"
        )
        membership = (
            protocol.hypothesis_version_id,
            strategy.specification_id,
            strategy.semantics_hash,
            comparison.canonical_hash,
            protocol.protocol_id,
            protocol.protocol_hash,
            tuple(run.run_id for run in concrete),
            tuple(run.result_hash for run in concrete),
            tuple(outcome.outcome_id for outcome in outcomes),
            tuple(run.prior_comparison.prior_run_id for run in concrete),
        )
        membership_hash = content_digest(membership)
        candidate = CryptoChallengerDossier(
            dossier_id=dossier_id,
            hypothesis_version_id=protocol.hypothesis_version_id,
            strategy_specification_id=strategy.specification_id,
            strategy_semantics_hash=strategy.semantics_hash,
            semantic_comparison_hash=comparison.canonical_hash,
            material_change_rationale=strategy.change_rationale,
            protocol_id=protocol.protocol_id,
            protocol_hash=protocol.protocol_hash,
            window_run_ids=(concrete[0].run_id, concrete[1].run_id),
            window_result_hashes=(concrete[0].result_hash, concrete[1].result_hash),
            window_outcome_ids=(outcomes[0].outcome_id, outcomes[1].outcome_id),
            prior_run_ids=(
                concrete[0].prior_comparison.prior_run_id,
                concrete[1].prior_comparison.prior_run_id,
            ),
            complete_windows=complete,
            passing_windows=passing,
            failing_windows=failing,
            inconclusive_windows=inconclusive,
            compounded_conservative_return=compounded,
            median_conservative_return=median,
            worst_window_return=returns[0],
            best_window_return=returns[1],
            worst_maximum_drawdown=max(metric.maximum_drawdown for metric in metrics),
            median_turnover=median_turnover,
            total_modeled_costs=sum(
                (metric.total_modeled_cost for metric in metrics), Decimal(0)
            ).quantize(Decimal("0.00000001")),
            total_entries=sum(metric.entries for metric in metrics),
            total_exits=sum(metric.exits for metric in metrics),
            percentage_windows_beating_cash=percentage_cash,
            percentage_windows_reducing_drawdown=percentage_drawdown,
            consistency_status=consistency,
            warnings=AUTHORITY_WARNINGS,
            membership_hash=membership_hash,
            created_at=created_at,
            canonical_hash=content_digest(
                (
                    CRYPTO_MONTHLY_CHALLENGER_DOSSIER_V1,
                    membership_hash,
                    complete,
                    passing,
                    failing,
                    inconclusive,
                    compounded,
                    median,
                    returns[0],
                    returns[1],
                    median_turnover,
                    consistency,
                    created_at,
                )
            ),
        )
        prior = self._repository.challenger_dossier(dossier_id)
        if prior is not None:
            if prior.membership_hash != candidate.membership_hash:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_DOSSIER_CONFLICT")
            return prior
        return self._repository.save_challenger_dossier(candidate)


class RecordCryptoChallengerConclusion:
    def __init__(self, repository: CryptoChallengerRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        conclusion_id: str,
        dossier_id: str,
        actor: str,
        decided_at: datetime,
    ) -> CryptoChallengerConclusion:
        dossier = self._repository.challenger_dossier(dossier_id)
        if dossier is None:
            raise CryptoResearchNotFound("CRYPTO_CHALLENGER_DOSSIER_NOT_FOUND")
        if dossier.inconclusive_windows:
            conclusion = ChallengerConclusionType.INCONCLUSIVE
            reason = "At least one window lacked valid complete reproducible evidence."
        elif dossier.passing_windows == 2 and dossier.compounded_conservative_return > 0:
            conclusion = ChallengerConclusionType.RESEARCH_CANDIDATE
            reason = (
                "Both genuine disjoint windows passed identical semantics and the "
                "compounded conservative return was positive; forward vintage "
                "observation remains required before any PAPER review."
            )
        elif dossier.failing_windows == 2:
            conclusion = ChallengerConclusionType.REJECTED
            reason = "Both complete genuine windows failed the predeclared policy."
        else:
            conclusion = ChallengerConclusionType.MIXED_EVIDENCE
            reason = "One complete genuine window passed and one failed the same policy."
        leading = (
            LEADING_RESEARCH_CHALLENGER_REQUIRES_FORWARD_VALIDATION
            if conclusion is ChallengerConclusionType.RESEARCH_CANDIDATE
            else NO_LEADING_CHALLENGER
        )
        payload = (
            CRYPTO_MONTHLY_CHALLENGER_CONCLUSION_V1,
            conclusion_id,
            dossier_id,
            conclusion,
            reason,
            leading,
            NO_APPROVED_CHAMPION,
            decided_at,
            actor,
            dossier.canonical_hash,
        )
        candidate = CryptoChallengerConclusion(
            conclusion_id,
            dossier_id,
            conclusion,
            reason,
            leading,
            NO_APPROVED_CHAMPION,
            decided_at,
            actor,
            content_digest(payload),
        )
        prior = self._repository.challenger_conclusion_for_dossier(dossier_id)
        if prior is not None:
            if prior.conclusion_id != conclusion_id:
                raise CryptoResearchConflict("CRYPTO_CHALLENGER_CONCLUSION_ALREADY_RECORDED")
            return prior
        return self._repository.save_challenger_conclusion(candidate)


def crypto_challenger_workspace(repository: CryptoChallengerRepository) -> dict[str, object]:
    from apex.application.crypto_dossier import crypto_dossier_workspace

    base = crypto_dossier_workspace(repository)
    conclusions = repository.challenger_conclusions()
    latest = conclusions[-1] if conclusions else None
    strategies = tuple(
        value
        for value in repository.strategy_specifications()
        if value.lifecycle_state is CryptoStrategyLifecycle.CHALLENGER_RESEARCH
    )
    base.update(
        {
            "challenger_hypotheses": repository.challenger_hypotheses(),
            "challenger_strategies": strategies,
            "challenger_protocols": repository.challenger_protocols(),
            "challenger_runs": repository.challenger_runs(),
            "challenger_outcomes": repository.challenger_outcomes(),
            "challenger_dossiers": repository.challenger_dossiers(),
            "challenger_conclusions": conclusions,
            "leading_challenger_status": (
                latest.leading_challenger_status if latest is not None else NO_LEADING_CHALLENGER
            ),
            "approved_champion_status": NO_APPROVED_CHAMPION,
            "research_state": (
                latest.conclusion.value if latest is not None else "CHALLENGER_NOT_EVALUATED"
            ),
            "network_behavior": "RETAINED_EVIDENCE_ONLY_NO_PROVIDER_REQUEST",
            "paper_eligible": False,
            "live_eligible": False,
            "capital_authority": "NONE",
            "opportunity_authority": "NONE",
            "order_authority": "NONE",
            "financial_mutations_possible": False,
        }
    )
    return base
