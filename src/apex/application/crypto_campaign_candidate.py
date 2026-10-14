"""Application services for crypto campaign Candidate 1."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.application.crypto_campaign import (
    CloseCampaignDevelopment,
    CryptoCampaignRepository,
    InMemoryCryptoCampaignRepository,
    NominateCampaignCandidate,
    RecordCampaignExperiment,
    RegisterCampaignCandidate,
    campaign_workspace,
    snapshot_access,
)
from apex.application.crypto_research import CryptoResearchConflict, CryptoResearchNotFound
from apex.domain.crypto_campaign import (
    CampaignBudgetStatus,
    CampaignCandidate,
    CampaignDatasetRole,
    CampaignExperimentUse,
    CampaignLifecycle,
    SnapshotUsePurpose,
    campaign_state,
    holdout_state,
)
from apex.domain.crypto_campaign_candidate import (
    AUTHORITY_WARNINGS,
    CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_V1,
    CRYPTO_CAMPAIGN_CANDIDATE_RESULT_V1,
    EARLIER_SNAPSHOT_ID,
    FIXED_WINDOWS,
    LATER_SNAPSHOT_ID,
    CandidateDossier,
    CandidateHypothesisSpecification,
    CandidateNominationEvaluation,
    CandidateOutcome,
    CandidatePortfolio,
    CandidateProtocol,
    CandidateRun,
    CandidateSemanticComparisons,
    CandidateState,
    PriorStrategyComparison,
    build_monthly_200_decisions,
    calculate_candidate_metrics,
    candidate_metric,
    candidate_window_passes,
)
from apex.domain.crypto_challenger import (
    ChallengerPortfolio,
    conservative_challenger_metric,
)
from apex.domain.crypto_dossier import (
    BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
    BTC_USD_SMA_20_100_STRATEGY_FAMILY,
    NO_APPROVED_CHAMPION,
    CryptoStrategySpecification,
    conservative_metric,
)
from apex.domain.crypto_research import (
    CryptoCaptureQuality,
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


class CryptoCampaignCandidateRepository(CryptoCampaignRepository, Protocol):
    def candidate_hypothesis(
        self, hypothesis_version_id: str
    ) -> CandidateHypothesisSpecification | None: ...
    def save_candidate_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: CandidateHypothesisSpecification,
    ) -> CandidateHypothesisSpecification: ...
    def candidate_hypotheses(self) -> tuple[CandidateHypothesisSpecification, ...]: ...
    def candidate_comparisons(self, candidate_id: str) -> CandidateSemanticComparisons | None: ...
    def save_candidate_comparisons(
        self, candidate_id: str, value: CandidateSemanticComparisons
    ) -> CandidateSemanticComparisons: ...
    def candidate_protocol(self, protocol_id: str) -> CandidateProtocol | None: ...
    def save_candidate_protocol(self, value: CandidateProtocol) -> CandidateProtocol: ...
    def candidate_protocols(self) -> tuple[CandidateProtocol, ...]: ...
    def candidate_run(self, run_id: str) -> CandidateRun | None: ...
    def save_candidate_run(self, value: CandidateRun) -> CandidateRun: ...
    def candidate_runs(self) -> tuple[CandidateRun, ...]: ...
    def candidate_outcome(self, outcome_id: str) -> CandidateOutcome | None: ...
    def candidate_outcome_for_run(self, run_id: str) -> CandidateOutcome | None: ...
    def save_candidate_outcome(self, value: CandidateOutcome) -> CandidateOutcome: ...
    def candidate_outcomes(self) -> tuple[CandidateOutcome, ...]: ...
    def candidate_dossier(self, dossier_id: str) -> CandidateDossier | None: ...
    def save_candidate_dossier(self, value: CandidateDossier) -> CandidateDossier: ...
    def candidate_dossiers(self) -> tuple[CandidateDossier, ...]: ...
    def candidate_nomination(self, evaluation_id: str) -> CandidateNominationEvaluation | None: ...
    def candidate_nomination_for_dossier(
        self, dossier_id: str
    ) -> CandidateNominationEvaluation | None: ...
    def save_candidate_nomination(
        self, value: CandidateNominationEvaluation
    ) -> CandidateNominationEvaluation: ...
    def candidate_nominations(self) -> tuple[CandidateNominationEvaluation, ...]: ...


class InMemoryCryptoCampaignCandidateRepository(InMemoryCryptoCampaignRepository):
    def __init__(self) -> None:
        super().__init__()
        self._candidate_hypotheses: dict[str, CandidateHypothesisSpecification] = {}
        self._candidate_comparisons: dict[str, CandidateSemanticComparisons] = {}
        self._candidate_protocols: dict[str, CandidateProtocol] = {}
        self._candidate_runs: dict[str, CandidateRun] = {}
        self._candidate_outcomes: dict[str, CandidateOutcome] = {}
        self._candidate_dossiers: dict[str, CandidateDossier] = {}
        self._candidate_nominations: dict[str, CandidateNominationEvaluation] = {}

    def candidate_hypothesis(
        self, hypothesis_version_id: str
    ) -> CandidateHypothesisSpecification | None:
        return self._candidate_hypotheses.get(hypothesis_version_id)

    def save_candidate_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: CandidateHypothesisSpecification,
    ) -> CandidateHypothesisSpecification:
        prior_hypothesis = self._hypotheses.get(hypothesis.hypothesis_version_id)
        prior = self.candidate_hypothesis(hypothesis.hypothesis_version_id)
        if (
            prior_hypothesis is not None
            and prior_hypothesis.canonical_hash != hypothesis.canonical_hash
        ) or (prior is not None and prior.canonical_hash != specification.canonical_hash):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_CONFLICT")
        self._hypotheses[hypothesis.hypothesis_version_id] = prior_hypothesis or hypothesis
        self._candidate_hypotheses[hypothesis.hypothesis_version_id] = prior or specification
        return prior or specification

    def candidate_hypotheses(self) -> tuple[CandidateHypothesisSpecification, ...]:
        return tuple(
            sorted(
                self._candidate_hypotheses.values(),
                key=lambda value: value.hypothesis_version_id,
            )
        )

    def candidate_comparisons(self, candidate_id: str) -> CandidateSemanticComparisons | None:
        return self._candidate_comparisons.get(candidate_id)

    def save_candidate_comparisons(
        self, candidate_id: str, value: CandidateSemanticComparisons
    ) -> CandidateSemanticComparisons:
        prior = self.candidate_comparisons(candidate_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_CONFLICT")
        self._candidate_comparisons[candidate_id] = prior or value
        return prior or value

    def candidate_protocol(self, protocol_id: str) -> CandidateProtocol | None:
        return self._candidate_protocols.get(protocol_id)

    def save_candidate_protocol(self, value: CandidateProtocol) -> CandidateProtocol:
        prior = self.candidate_protocol(value.protocol_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_CONFLICT")
        self._candidate_protocols[value.protocol_id] = prior or value
        return prior or value

    def candidate_protocols(self) -> tuple[CandidateProtocol, ...]:
        return tuple(
            sorted(self._candidate_protocols.values(), key=lambda value: value.protocol_id)
        )

    def candidate_run(self, run_id: str) -> CandidateRun | None:
        return self._candidate_runs.get(run_id)

    def save_candidate_run(self, value: CandidateRun) -> CandidateRun:
        prior = self.candidate_run(value.run_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_RUN_CONFLICT")
        duplicate = next(
            (
                item
                for item in self._candidate_runs.values()
                if item.protocol_id == value.protocol_id
                and item.snapshot_id == value.snapshot_id
                and item.run_id != value.run_id
            ),
            None,
        )
        if duplicate is not None:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_WINDOW_ALREADY_EVALUATED")
        self._candidate_runs[value.run_id] = prior or value
        return prior or value

    def candidate_runs(self) -> tuple[CandidateRun, ...]:
        return tuple(sorted(self._candidate_runs.values(), key=lambda value: value.run_id))

    def candidate_outcome(self, outcome_id: str) -> CandidateOutcome | None:
        return self._candidate_outcomes.get(outcome_id)

    def candidate_outcome_for_run(self, run_id: str) -> CandidateOutcome | None:
        return next(
            (value for value in self._candidate_outcomes.values() if value.run_id == run_id),
            None,
        )

    def save_candidate_outcome(self, value: CandidateOutcome) -> CandidateOutcome:
        prior = self.candidate_outcome(value.outcome_id)
        by_run = self.candidate_outcome_for_run(value.run_id)
        if (prior is not None and prior.canonical_hash != value.canonical_hash) or (
            by_run is not None and by_run.canonical_hash != value.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_OUTCOME_CONFLICT")
        self._candidate_outcomes[value.outcome_id] = prior or value
        return prior or value

    def candidate_outcomes(self) -> tuple[CandidateOutcome, ...]:
        return tuple(sorted(self._candidate_outcomes.values(), key=lambda value: value.outcome_id))

    def candidate_dossier(self, dossier_id: str) -> CandidateDossier | None:
        return self._candidate_dossiers.get(dossier_id)

    def save_candidate_dossier(self, value: CandidateDossier) -> CandidateDossier:
        prior = self.candidate_dossier(value.dossier_id)
        if prior is not None and prior.canonical_hash != value.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_CONFLICT")
        self._candidate_dossiers[value.dossier_id] = prior or value
        return prior or value

    def candidate_dossiers(self) -> tuple[CandidateDossier, ...]:
        return tuple(sorted(self._candidate_dossiers.values(), key=lambda value: value.dossier_id))

    def candidate_nomination(self, evaluation_id: str) -> CandidateNominationEvaluation | None:
        return self._candidate_nominations.get(evaluation_id)

    def candidate_nomination_for_dossier(
        self, dossier_id: str
    ) -> CandidateNominationEvaluation | None:
        return next(
            (
                value
                for value in self._candidate_nominations.values()
                if value.dossier_id == dossier_id
            ),
            None,
        )

    def save_candidate_nomination(
        self, value: CandidateNominationEvaluation
    ) -> CandidateNominationEvaluation:
        prior = self.candidate_nomination(value.evaluation_id)
        by_dossier = self.candidate_nomination_for_dossier(value.dossier_id)
        if (prior is not None and prior.canonical_hash != value.canonical_hash) or (
            by_dossier is not None and by_dossier.canonical_hash != value.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_CONFLICT")
        self._candidate_nominations[value.evaluation_id] = prior or value
        return prior or value

    def candidate_nominations(self) -> tuple[CandidateNominationEvaluation, ...]:
        return tuple(
            sorted(self._candidate_nominations.values(), key=lambda value: value.evaluation_id)
        )


def _single_strategy(
    repository: CryptoCampaignCandidateRepository, family: str
) -> CryptoStrategySpecification:
    matches = tuple(
        item for item in repository.strategy_specifications() if item.strategy_family == family
    )
    if len(matches) != 1:
        raise CryptoResearchConflict(f"CRYPTO_CAMPAIGN_CANDIDATE_PRIOR_FAMILY_INVALID:{family}")
    return matches[0]


class RegisterCandidateHypothesis:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        hypothesis_id: str,
        campaign_id: str,
        actor: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> tuple[ResearchHypothesisVersion, CandidateHypothesisSpecification]:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        campaign = self._repository.campaign(campaign_id)
        if campaign is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_NOT_FOUND")
        if (
            campaign_state(campaign, self._repository.campaign_events(campaign_id))
            is not CampaignLifecycle.DEVELOPMENT_ACTIVE
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_NOT_DEVELOPMENT_ACTIVE")
        prior_revision = self._repository.hypothesis_revision(hypothesis_id, 1)
        if prior_revision is not None:
            specification = self._repository.candidate_hypothesis(
                prior_revision.hypothesis_version_id
            )
            if specification is None:
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_CONFLICT")
            return prior_revision, specification
        hypothesis = ResearchHypothesisVersion.create(
            hypothesis_id=hypothesis_id,
            revision=1,
            question=(
                "Will the predeclared monthly BTC-USD close-versus-200-observation-mean "
                "long-or-cash rule produce non-negative conservative returns and lower "
                "maximum drawdown than buy-and-hold in both approved development windows?"
            ),
            motivation=(
                "Test one materially different medium-horizon path-sensitive trend filter "
                "on only the campaign's exposed development evidence. No authority."
            ),
            actor=actor,
            market_scope=(
                campaign_id,
                "BTC-USD",
                "DAILY_OHLCV",
                "MONTHLY_200_OBSERVATION_PRICE_TREND",
                EARLIER_SNAPSHOT_ID,
                LATER_SNAPSHOT_ID,
                "EXPOSED_DEVELOPMENT_EVIDENCE_NOT_INDEPENDENT_VALIDATION",
                "AUTHORITY_NONE",
            ),
            created_at=created_at,
            status=HypothesisStatus.REGISTERED,
        )
        specification = CandidateHypothesisSpecification.fixed(hypothesis, campaign_id=campaign_id)
        self._repository.save_candidate_hypothesis(hypothesis, specification)
        return hypothesis, specification


class RegisterCandidateStrategy:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        specification_id: str,
        hypothesis_version_id: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> tuple[CryptoStrategySpecification, CandidateSemanticComparisons]:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        hypothesis = self._repository.candidate_hypothesis(hypothesis_version_id)
        if hypothesis is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_NOT_FOUND")
        sma = _single_strategy(self._repository, BTC_USD_SMA_20_100_STRATEGY_FAMILY)
        monthly = _single_strategy(self._repository, BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY)
        candidate = CryptoStrategySpecification.monthly_200_price_trend_candidate(
            specification_id=specification_id,
            hypothesis_version_id=hypothesis_version_id,
            parent=monthly,
            created_at=created_at,
        )
        prior = self._repository.strategy_specification(specification_id)
        if prior is not None:
            if prior.semantics_hash != candidate.semantics_hash:
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_STRATEGY_CONFLICT")
            return prior, CandidateSemanticComparisons.create(
                candidate=prior, sma=sma, monthly_252=monthly
            )
        duplicate = self._repository.strategy_by_semantics(candidate.semantics_hash)
        if duplicate is not None:
            raise CryptoResearchConflict(
                f"CRYPTO_DUPLICATE_STRATEGY_ALREADY_TESTED:{duplicate.specification_id}"
            )
        self._repository.save_strategy_specification(candidate)
        return candidate, CandidateSemanticComparisons.create(
            candidate=candidate, sma=sma, monthly_252=monthly
        )


class RegisterCandidate:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        candidate_id: str,
        campaign_id: str,
        hypothesis_version_id: str,
        strategy_specification_id: str,
        actor: str,
        registered_at: datetime,
        idempotency_key: str,
    ) -> CampaignCandidate:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        prior = self._repository.campaign_candidate(candidate_id)
        if prior is not None:
            if (
                prior.campaign_id != campaign_id
                or prior.hypothesis_version_id != hypothesis_version_id
                or prior.strategy_specification_id != strategy_specification_id
            ):
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_CONFLICT")
            return prior
        hypothesis = self._repository.candidate_hypothesis(hypothesis_version_id)
        strategy = self._repository.strategy_specification(strategy_specification_id)
        if hypothesis is None or strategy is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_REGISTRATION_INPUT_NOT_FOUND")
        if hypothesis.campaign_id != campaign_id or strategy.hypothesis_version_id != (
            hypothesis_version_id
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_LINEAGE_INVALID")
        sma = _single_strategy(self._repository, BTC_USD_SMA_20_100_STRATEGY_FAMILY)
        monthly = _single_strategy(self._repository, BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY)
        comparisons = CandidateSemanticComparisons.create(
            candidate=strategy, sma=sma, monthly_252=monthly
        )
        value = CampaignCandidate.create(
            candidate_id=candidate_id,
            campaign_id=campaign_id,
            hypothesis_version_id=hypothesis_version_id,
            strategy_specification_id=strategy_specification_id,
            strategy_family=strategy.strategy_family,
            strategy_semantics_hash=strategy.semantics_hash,
            rationale=strategy.change_rationale,
            expected_mechanism=hypothesis.expected_mechanism,
            failure_conditions=hypothesis.failure_conditions,
            exact_configuration_hash=content_digest(
                (hypothesis.canonical_hash, strategy.canonical_hash, comparisons.canonical_hash)
            ),
            registered_at=registered_at,
            actor=actor,
        )
        retained = RegisterCampaignCandidate(self._repository).execute(
            value, idempotency_key=idempotency_key
        )
        self._repository.save_candidate_comparisons(candidate_id, comparisons)
        return retained


class RegisterCandidateProtocol:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        protocol_id: str,
        campaign_id: str,
        candidate_id: str,
        code_revision: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> CandidateProtocol:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        prior = self._repository.candidate_protocol(protocol_id)
        if prior is not None:
            if (
                prior.campaign_id != campaign_id
                or prior.candidate_id != candidate_id
                or prior.code_revision != code_revision
            ):
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_CONFLICT")
            return prior
        candidate = self._repository.campaign_candidate(candidate_id)
        if candidate is None or candidate.campaign_id != campaign_id:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_NOT_FOUND")
        hypothesis = self._repository.candidate_hypothesis(candidate.hypothesis_version_id)
        strategy = self._repository.strategy_specification(candidate.strategy_specification_id)
        attachments = self._repository.campaign_attachments(campaign_id)
        attached = {
            item.snapshot_id: item
            for item in attachments
            if item.role is CampaignDatasetRole.DEVELOPMENT
        }
        if hypothesis is None or strategy is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_INPUT_NOT_FOUND")
        for window in FIXED_WINDOWS:
            snapshot = self._repository.snapshot(window.snapshot_id)
            attachment = attached.get(window.snapshot_id)
            if (
                snapshot is None
                or snapshot.evidence_class is not EvidenceClass.HISTORICAL_PUBLIC
                or snapshot.quality is not CryptoCaptureQuality.VALID
                or attachment is None
                or attachment.exposure_state_at_attachment != "EXPOSED_DEVELOPMENT_EVIDENCE"
            ):
                raise CryptoResearchConflict(
                    "CRYPTO_CAMPAIGN_CANDIDATE_APPROVED_DEVELOPMENT_EVIDENCE_REQUIRED"
                )
        value = CandidateProtocol.fixed(
            protocol_id=protocol_id,
            campaign_id=campaign_id,
            candidate_id=candidate_id,
            hypothesis=hypothesis,
            strategy=strategy,
            code_revision=code_revision,
            created_at=created_at,
        )
        return self._repository.save_candidate_protocol(value)


class ExecuteCandidateWindow:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        consumption_id: str,
        protocol_id: str,
        snapshot_id: str,
        code_revision: str,
        actor: str,
        executed_at: datetime,
        idempotency_key: str,
    ) -> CandidateRun:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        prior = self._repository.candidate_run(run_id)
        if prior is not None:
            usage = self._repository.campaign_experiment(consumption_id)
            if (
                prior.protocol_id != protocol_id
                or prior.snapshot_id != snapshot_id
                or prior.code_revision != code_revision
                or usage is None
                or usage.run_id != run_id
                or usage.candidate_id != prior.candidate_id
                or usage.snapshot_id != snapshot_id
            ):
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_RUN_CONFLICT")
            return prior
        value = self._build(
            run_id=run_id,
            protocol_id=protocol_id,
            snapshot_id=snapshot_id,
            code_revision=code_revision,
            executed_at=executed_at,
        )
        retained = self._repository.save_candidate_run(value)
        usage = CampaignExperimentUse.create(
            consumption_id=consumption_id,
            campaign_id=value.campaign_id,
            candidate_id=value.candidate_id,
            run_id=value.run_id,
            snapshot_id=value.snapshot_id,
            snapshot_exposure_state=value.snapshot_exposure_state,
            strategy_semantics_hash=value.strategy_semantics_hash,
            use_purpose=SnapshotUsePurpose.DEVELOPMENT,
            complete=value.complete,
            reproducible=(value.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE),
            window_policy_passed=candidate_window_passes(value),
            result_hash=value.result_hash,
            recorded_at=executed_at,
            actor=actor,
        )
        RecordCampaignExperiment(self._repository).execute(usage, idempotency_key=idempotency_key)
        return retained

    def replay(self, run_id: str) -> CandidateRun:
        prior = self._repository.candidate_run(run_id)
        if prior is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_RUN_NOT_FOUND")
        replay = self._build(
            run_id=prior.run_id,
            protocol_id=prior.protocol_id,
            snapshot_id=prior.snapshot_id,
            code_revision=prior.code_revision,
            executed_at=prior.executed_at,
        )
        if replay.result_hash != prior.result_hash or replay.canonical_hash != prior.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_REPLAY_MISMATCH")
        return prior

    def _build(
        self,
        *,
        run_id: str,
        protocol_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CandidateRun:
        protocol = self._repository.candidate_protocol(protocol_id)
        snapshot = self._repository.snapshot(snapshot_id)
        if protocol is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_NOT_FOUND")
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_SNAPSHOT_NOT_FOUND")
        if code_revision != protocol.code_revision:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_CODE_REVISION_MISMATCH")
        access = snapshot_access(
            self._repository,
            snapshot_id=snapshot_id,
            purpose=SnapshotUsePurpose.DEVELOPMENT,
            campaign_id=protocol.campaign_id,
            candidate_id=protocol.candidate_id,
        )
        if not access.allowed:
            raise CryptoResearchConflict(access.reason)
        try:
            window = next(item for item in protocol.windows if item.snapshot_id == snapshot_id)
        except StopIteration as error:
            raise CryptoResearchConflict(
                "CRYPTO_CAMPAIGN_CANDIDATE_SNAPSHOT_NOT_IN_PROTOCOL"
            ) from error
        candidate = self._repository.campaign_candidate(protocol.candidate_id)
        if candidate is None or candidate.registered_at >= executed_at:
            raise CryptoResearchConflict(
                "CRYPTO_CAMPAIGN_CANDIDATE_MUST_BE_REGISTERED_BEFORE_EXECUTION"
            )
        expected_count = len(expected_bucket_starts(snapshot.start, snapshot.end))
        complete = (
            snapshot.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
            and snapshot.quality is CryptoCaptureQuality.VALID
            and snapshot.accepted_buckets == expected_count
            and snapshot.expected_buckets == expected_count
            and not snapshot.missing_bucket_starts
            and snapshot.rejected_records == 0
            and snapshot.duplicate_records == 0
            and access.exposure_state == "EXPOSED_DEVELOPMENT_EVIDENCE"
        )
        candles = self._repository.candles(snapshot.capture_id)
        decisions = build_monthly_200_decisions(
            candles,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
        )
        metrics = calculate_candidate_metrics(
            candles=candles,
            decisions=decisions,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
        )
        candidate_metric_value = candidate_metric(
            metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND
        )
        sma_run = (
            self._repository.replication_run("btc-usd-genuine-disjoint-replication-v1")
            if snapshot_id == EARLIER_SNAPSHOT_ID
            else self._repository.run("btc-usd-genuine-trend-validation-v1")
        )
        monthly_runs = tuple(
            item for item in self._repository.challenger_runs() if item.snapshot_id == snapshot_id
        )
        if sma_run is None or len(monthly_runs) != 1:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_PRIOR_RESULT_NOT_FOUND")
        monthly_run = monthly_runs[0]
        sma_metric = conservative_metric(sma_run.metrics, PortfolioName.TREND_RULE)
        monthly_metric = conservative_challenger_metric(
            monthly_run.metrics, ChallengerPortfolio.MONTHLY_MOMENTUM
        )
        comparisons = (
            PriorStrategyComparison.create(
                strategy_family=BTC_USD_SMA_20_100_STRATEGY_FAMILY,
                prior_run_id=sma_run.run_id,
                prior_result_hash=sma_run.result_hash,
                prior_canonical_run_hash=sma_run.canonical_hash,
                prior_net_return=sma_metric.net_return,
                prior_drawdown=sma_metric.maximum_drawdown,
                prior_turnover=sma_metric.total_turnover,
                prior_cost=sma_metric.cost_paid,
                candidate=candidate_metric_value,
            ),
            PriorStrategyComparison.create(
                strategy_family=BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
                prior_run_id=monthly_run.run_id,
                prior_result_hash=monthly_run.result_hash,
                prior_canonical_run_hash=monthly_run.canonical_hash,
                prior_net_return=monthly_metric.net_return,
                prior_drawdown=monthly_metric.maximum_drawdown,
                prior_turnover=monthly_metric.turnover,
                prior_cost=monthly_metric.total_modeled_cost,
                candidate=candidate_metric_value,
            ),
        )
        if any(not item.eligible for item in decisions):
            complete = False
        state = ExperimentState.COMPLETED if complete else ExperimentState.FAILED
        reproducibility = (
            ReproducibilityStatus.REPRODUCIBLE
            if complete
            else ReproducibilityStatus.NOT_REPRODUCIBLE
        )
        failure_reason = (
            None
            if complete
            else "CRYPTO_CAMPAIGN_CANDIDATE_REQUIRED_DEVELOPMENT_EVIDENCE_INCOMPLETE"
        )
        configuration_hash = content_digest(
            (
                protocol.protocol_hash,
                protocol.strategy_semantics_hash,
                snapshot.manifest_hash,
                access.exposure_state,
                window.validation_start,
                window.validation_end,
                tuple(item.prior_result_hash for item in comparisons),
            )
        )
        result_hash = content_digest(
            (
                CRYPTO_CAMPAIGN_CANDIDATE_RESULT_V1,
                configuration_hash,
                decisions,
                metrics,
                comparisons,
                AUTHORITY_WARNINGS,
                (),
                complete,
                state,
                reproducibility,
                failure_reason,
            )
        )
        canonical_hash = content_digest(
            (
                run_id,
                protocol.campaign_id,
                protocol.candidate_id,
                protocol_id,
                snapshot_id,
                code_revision,
                executed_at,
                result_hash,
            )
        )
        return CandidateRun(
            run_id,
            protocol.campaign_id,
            protocol.candidate_id,
            protocol_id,
            protocol.hypothesis_version_id,
            protocol.strategy_specification_id,
            protocol.strategy_semantics_hash,
            snapshot_id,
            snapshot.manifest_hash,
            access.exposure_state,
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
            comparisons,
            AUTHORITY_WARNINGS,
            (),
            failure_reason,
            configuration_hash,
            result_hash,
            canonical_hash,
        )


class RecordCandidateOutcome:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        outcome_id: str,
        run_id: str,
        actor: str,
        decided_at: datetime,
        idempotency_key: str,
    ) -> CandidateOutcome:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        run = self._repository.candidate_run(run_id)
        if run is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_RUN_NOT_FOUND")
        prior = self._repository.candidate_outcome_for_run(run_id)
        if prior is not None:
            if prior.outcome_id != outcome_id:
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_OUTCOME_ALREADY_RECORDED")
            return prior
        metric = candidate_metric(run.metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND)
        baseline = candidate_metric(run.metrics, CandidatePortfolio.BUY_AND_HOLD)
        conditions = (
            ("CONSERVATIVE_NET_RETURN_AT_LEAST_ZERO", metric.net_return >= 0),
            (
                "DRAWDOWN_STRICTLY_BELOW_BUY_AND_HOLD",
                metric.maximum_drawdown < baseline.maximum_drawdown,
            ),
            ("EVIDENCE_COMPLETE", run.complete),
            (
                "REPRODUCIBLE",
                run.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE,
            ),
            ("NO_DISQUALIFYING_WARNING", not run.disqualifying_warnings),
            ("AT_LEAST_ONE_LONG_EXPOSURE_PERIOD", metric.days_long > 0),
        )
        passed = tuple(name for name, value in conditions if value)
        failed = tuple(name for name, value in conditions if not value)
        if not run.complete or run.reproducibility_status is not ReproducibilityStatus.REPRODUCIBLE:
            decision = ExperimentOutcomeDecision.DEFERRED
            reason = "Incomplete or nonreproducible evidence is inconclusive."
        elif not failed:
            decision = ExperimentOutcomeDecision.PROMOTION_CANDIDATE
            reason = (
                "Every predeclared per-window development criterion passed; authority remains none."
            )
        else:
            decision = ExperimentOutcomeDecision.REJECTED
            reason = (
                "Complete development evidence contradicted at least one predeclared criterion."
            )
        payload = (
            outcome_id,
            run_id,
            decision,
            reason,
            passed,
            failed,
            decided_at,
            actor,
            run.result_hash,
        )
        return self._repository.save_candidate_outcome(
            CandidateOutcome(*payload[:-1], content_digest(payload))
        )


class MaterializeCandidateDossier:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        dossier_id: str,
        campaign_id: str,
        candidate_id: str,
        protocol_id: str,
        earlier_outcome_id: str,
        later_outcome_id: str,
        created_at: datetime,
        idempotency_key: str,
    ) -> CandidateDossier:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        prior = self._repository.candidate_dossier(dossier_id)
        if prior is not None:
            if (
                prior.campaign_id != campaign_id
                or prior.candidate_id != candidate_id
                or prior.protocol_id != protocol_id
                or prior.window_outcome_ids != (earlier_outcome_id, later_outcome_id)
            ):
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_CONFLICT")
            return prior
        candidate = self._repository.campaign_candidate(candidate_id)
        protocol = self._repository.candidate_protocol(protocol_id)
        comparisons = self._repository.candidate_comparisons(candidate_id)
        outcomes = (
            self._repository.candidate_outcome(earlier_outcome_id),
            self._repository.candidate_outcome(later_outcome_id),
        )
        if (
            candidate is None
            or protocol is None
            or comparisons is None
            or any(item is None for item in outcomes)
        ):
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_INPUT_NOT_FOUND")
        concrete_outcomes = tuple(item for item in outcomes if item is not None)
        runs = tuple(self._repository.candidate_run(item.run_id) for item in concrete_outcomes)
        if any(item is None for item in runs):
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_RUN_NOT_FOUND")
        by_snapshot = {item.snapshot_id: item for item in runs if item is not None}
        if set(by_snapshot) != {EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID}:
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_WINDOW_MISMATCH")
        concrete = (by_snapshot[EARLIER_SNAPSHOT_ID], by_snapshot[LATER_SNAPSHOT_ID])
        if (
            candidate.campaign_id != campaign_id
            or protocol.campaign_id != campaign_id
            or protocol.candidate_id != candidate_id
            or any(item.protocol_id != protocol_id for item in concrete)
            or len({item.strategy_semantics_hash for item in concrete}) != 1
        ):
            raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_LINEAGE_INVALID")
        metrics = tuple(
            candidate_metric(item.metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND)
            for item in concrete
        )
        baselines = tuple(
            candidate_metric(item.metrics, CandidatePortfolio.BUY_AND_HOLD) for item in concrete
        )
        ordered_outcomes = tuple(
            next(item for item in concrete_outcomes if item.run_id == run.run_id)
            for run in concrete
        )
        passing = sum(
            item.decision is ExperimentOutcomeDecision.PROMOTION_CANDIDATE
            for item in ordered_outcomes
        )
        failing = sum(
            item.decision is ExperimentOutcomeDecision.REJECTED for item in ordered_outcomes
        )
        inconclusive = 2 - passing - failing
        returns = sorted(item.net_return for item in metrics)
        turnovers = sorted(item.turnover for item in metrics)
        compounded = ((Decimal(1) + returns[0]) * (Decimal(1) + returns[1]) - Decimal(1)).quantize(
            Decimal("0.00000001")
        )
        median = ((returns[0] + returns[1]) / Decimal(2)).quantize(Decimal("0.00000001"))
        median_turnover = ((turnovers[0] + turnovers[1]) / Decimal(2)).quantize(
            Decimal("0.00000001")
        )

        def percentage(count: int) -> Decimal:
            return (Decimal(count) / Decimal(2) * Decimal(100)).quantize(Decimal("0.00000001"))

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
            campaign_id,
            candidate_id,
            candidate.canonical_hash,
            protocol.hypothesis_version_id,
            protocol.strategy_specification_id,
            protocol.strategy_semantics_hash,
            comparisons.canonical_hash,
            protocol_id,
            protocol.protocol_hash,
            tuple(item.snapshot_id for item in concrete),
            tuple(item.snapshot_manifest_hash for item in concrete),
            tuple(item.snapshot_exposure_state for item in concrete),
            tuple(item.run_id for item in concrete),
            tuple(item.result_hash for item in concrete),
            tuple(item.outcome_id for item in ordered_outcomes),
        )
        membership_hash = content_digest(membership)
        payload = (
            CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_V1,
            dossier_id,
            membership_hash,
            sum(item.complete for item in concrete),
            passing,
            failing,
            inconclusive,
            compounded,
            median,
            returns[0],
            returns[1],
            max(item.maximum_drawdown for item in metrics),
            median_turnover,
            sum((item.modeled_fees for item in metrics), Decimal(0)),
            sum((item.modeled_slippage for item in metrics), Decimal(0)),
            sum((item.total_modeled_cost for item in metrics), Decimal(0)),
            sum(item.entries for item in metrics),
            sum(item.exits for item in metrics),
            percentage(sum(item.net_return > 0 for item in metrics)),
            percentage(
                sum(
                    item.maximum_drawdown < baseline.maximum_drawdown
                    for item, baseline in zip(metrics, baselines, strict=True)
                )
            ),
            percentage(
                sum(run.prior_comparisons[0].candidate_net_return_delta > 0 for run in concrete)
            ),
            percentage(
                sum(run.prior_comparisons[1].candidate_net_return_delta > 0 for run in concrete)
            ),
            consistency,
            AUTHORITY_WARNINGS,
            False,
            False,
            created_at,
        )
        value = CandidateDossier(
            dossier_id,
            campaign_id,
            candidate_id,
            candidate.canonical_hash,
            protocol.hypothesis_version_id,
            protocol.strategy_specification_id,
            protocol.strategy_semantics_hash,
            comparisons.canonical_hash,
            protocol_id,
            protocol.protocol_hash,
            (concrete[0].snapshot_id, concrete[1].snapshot_id),
            (
                concrete[0].snapshot_manifest_hash,
                concrete[1].snapshot_manifest_hash,
            ),
            (
                concrete[0].snapshot_exposure_state,
                concrete[1].snapshot_exposure_state,
            ),
            (concrete[0].run_id, concrete[1].run_id),
            (concrete[0].result_hash, concrete[1].result_hash),
            (ordered_outcomes[0].outcome_id, ordered_outcomes[1].outcome_id),
            *payload[3:-1],
            membership_hash,
            created_at,
            content_digest(payload),
        )
        return self._repository.save_candidate_dossier(value)


class ApplyCandidateNominationPolicy:
    def __init__(self, repository: CryptoCampaignCandidateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        evaluation_id: str,
        dossier_id: str,
        nomination_event_id: str,
        development_closed_event_id: str,
        actor: str,
        reason: str,
        evaluated_at: datetime,
        idempotency_key: str,
    ) -> CandidateNominationEvaluation:
        if not idempotency_key.strip():
            raise CryptoResearchConflict("IDEMPOTENCY_KEY_REQUIRED")
        prior = self._repository.candidate_nomination_for_dossier(dossier_id)
        if prior is not None:
            if prior.evaluation_id != evaluation_id:
                raise CryptoResearchConflict("CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_CONFLICT")
            return prior
        dossier = self._repository.candidate_dossier(dossier_id)
        if dossier is None:
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_NOT_FOUND")
        campaign = self._repository.campaign(dossier.campaign_id)
        candidate = self._repository.campaign_candidate(dossier.candidate_id)
        comparisons = self._repository.candidate_comparisons(dossier.candidate_id)
        runs = tuple(self._repository.candidate_run(run_id) for run_id in dossier.window_run_ids)
        if (
            campaign is None
            or candidate is None
            or comparisons is None
            or any(item is None for item in runs)
        ):
            raise CryptoResearchNotFound("CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_INPUT_NOT_FOUND")
        concrete = tuple(item for item in runs if item is not None)
        budget = CampaignBudgetStatus.calculate(
            campaign,
            self._repository.campaign_candidates(campaign.campaign_id),
            self._repository.campaign_experiments(campaign.campaign_id),
        )
        events = self._repository.campaign_events(campaign.campaign_id)
        conditions = (
            (
                "REGISTERED_BEFORE_BOTH_RUNS",
                all(candidate.registered_at < item.executed_at for item in concrete),
            ),
            (
                "MATERIALLY_DISTINCT_FROM_BOTH_REJECTED_STRATEGIES",
                comparisons.against_sma_20_100.materially_distinct
                and comparisons.against_monthly_252.materially_distinct,
            ),
            (
                "CAMPAIGN_BUDGET_VALID",
                budget.consumed_candidates <= budget.candidate_budget
                and budget.consumed_experiments <= budget.experiment_budget,
            ),
            (
                "ONLY_APPROVED_EXPOSED_DEVELOPMENT_DATA_USED",
                {item.snapshot_id for item in concrete} == {EARLIER_SNAPSHOT_ID, LATER_SNAPSHOT_ID}
                and all(
                    item.snapshot_exposure_state == "EXPOSED_DEVELOPMENT_EVIDENCE"
                    for item in concrete
                ),
            ),
            (
                "IDENTICAL_SEMANTICS_ACROSS_WINDOWS",
                len({item.strategy_semantics_hash for item in concrete}) == 1
                and concrete[0].strategy_semantics_hash == candidate.strategy_semantics_hash,
            ),
            (
                "BOTH_RUNS_COMPLETE_AND_REPRODUCIBLE",
                len(concrete) == 2
                and all(
                    item.complete
                    and item.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE
                    for item in concrete
                ),
            ),
            ("BOTH_WINDOWS_PASS", dossier.passing_windows == 2),
            (
                "BOTH_CONSERVATIVE_RETURNS_NON_NEGATIVE",
                all(
                    candidate_metric(
                        item.metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND
                    ).net_return
                    >= 0
                    for item in concrete
                ),
            ),
            (
                "BOTH_DRAWDOWNS_STRICTLY_LOWER_THAN_BUY_AND_HOLD",
                all(
                    candidate_metric(
                        item.metrics, CandidatePortfolio.MONTHLY_200_PRICE_TREND
                    ).maximum_drawdown
                    < candidate_metric(
                        item.metrics, CandidatePortfolio.BUY_AND_HOLD
                    ).maximum_drawdown
                    for item in concrete
                ),
            ),
            (
                "NO_LEAKAGE_OR_QUALITY_DISQUALIFICATION",
                all(not item.disqualifying_warnings for item in concrete),
            ),
            (
                "COMPOUNDED_CONSERVATIVE_RETURN_POSITIVE",
                dossier.compounded_conservative_return > 0,
            ),
            (
                "NO_PRIOR_NOMINATION",
                campaign_state(campaign, events) is CampaignLifecycle.DEVELOPMENT_ACTIVE,
            ),
            ("SEALED_HOLDOUT_NOT_ACCESSED", not dossier.holdout_accessed),
        )
        passed = tuple(name for name, value in conditions if value)
        failed = tuple(name for name, value in conditions if not value)
        eligible = not failed
        value = CandidateNominationEvaluation(
            evaluation_id,
            dossier_id,
            dossier.campaign_id,
            dossier.candidate_id,
            conditions,
            passed,
            failed,
            eligible,
            CandidateState.NOMINATION_ELIGIBLE
            if eligible
            else CandidateState.NOT_NOMINATION_ELIGIBLE,
            evaluated_at,
            actor,
            reason,
            content_digest(
                (
                    evaluation_id,
                    dossier.canonical_hash,
                    conditions,
                    eligible,
                    evaluated_at,
                    actor,
                    reason,
                )
            ),
        )
        retained = self._repository.save_candidate_nomination(value)
        if eligible:
            NominateCampaignCandidate(self._repository).execute(
                event_id=nomination_event_id,
                campaign_id=dossier.campaign_id,
                candidate_id=dossier.candidate_id,
                actor=actor,
                reason=reason,
                nominated_at=evaluated_at,
                idempotency_key=idempotency_key + ":nominate",
            )
            CloseCampaignDevelopment(self._repository).execute(
                event_id=development_closed_event_id,
                campaign_id=dossier.campaign_id,
                candidate_id=dossier.candidate_id,
                actor=actor,
                reason=(
                    "Candidate nomination closed development; holdout release remains unauthorized."
                ),
                closed_at=evaluated_at,
                idempotency_key=idempotency_key + ":close",
            )
        return retained


def candidate_workspace(
    repository: CryptoCampaignCandidateRepository,
) -> dict[str, object]:
    base = campaign_workspace(repository)
    campaigns = repository.campaigns()
    campaign = campaigns[0] if len(campaigns) == 1 else None
    candidates = (
        repository.campaign_candidates(campaign.campaign_id) if campaign is not None else ()
    )
    experiments = (
        repository.campaign_experiments(campaign.campaign_id) if campaign is not None else ()
    )
    events = repository.campaign_events(campaign.campaign_id) if campaign is not None else ()
    nominations = repository.candidate_nominations()
    nomination = nominations[0] if len(nominations) == 1 else None
    state = campaign_state(campaign, events) if campaign is not None else None
    leading = (
        "NOMINATED_RESEARCH_CHALLENGER_REQUIRES_SEALED_HOLDOUT_GOVERNANCE"
        if state is CampaignLifecycle.DEVELOPMENT_CLOSED and nomination is not None
        else "NO_LEADING_CHALLENGER"
    )
    base.update(
        {
            "candidate_1_hypotheses": repository.candidate_hypotheses(),
            "campaign_candidates": candidates,
            "candidate_1_semantic_comparisons": tuple(
                value
                for item in candidates
                if (value := repository.candidate_comparisons(item.candidate_id)) is not None
            ),
            "candidate_1_protocols": repository.candidate_protocols(),
            "candidate_1_runs": repository.candidate_runs(),
            "candidate_1_outcomes": repository.candidate_outcomes(),
            "candidate_1_dossiers": repository.candidate_dossiers(),
            "candidate_1_nomination_evaluations": nominations,
            "candidate_1_nomination_eligible": nomination.eligible
            if nomination is not None
            else False,
            "candidate_1_failed_nomination_conditions": nomination.failed_conditions
            if nomination is not None
            else (),
            "campaign_budget": CampaignBudgetStatus.calculate(campaign, candidates, experiments)
            if campaign is not None
            else None,
            "campaign_state": state,
            "leading_challenger_status": leading,
            "approved_champion_status": NO_APPROVED_CHAMPION,
            "holdout_non_use": {
                "state": holdout_state(repository.holdout_events(campaign.holdout_id))
                if campaign is not None
                else None,
                "release_authorized": False,
                "evaluated": False,
                "values_redacted": True,
            },
            "candidate_evidence_class": "EXPOSED_DEVELOPMENT_EVIDENCE",
            "independent_validation": False,
            "paper_eligible": False,
            "live_eligible": False,
            "capital_authority": "NONE",
            "opportunity_authority": "NONE",
            "order_authority": "NONE",
            "execution_authority": "NONE",
            "broker_authority": "NONE",
            "financial_authority": "NONE",
            "financial_mutation_capability": "NONE",
        }
    )
    return base
