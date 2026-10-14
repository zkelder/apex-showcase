"""Local-only FastAPI boundary for the Apex Operator Console."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
from apex.adapters.batch_learning_operations import (
    BATCH_LEARNING_WARNINGS,
    MANAGED_RETAINED_SOURCE_SCOPE,
    BatchLearningRuntime,
    PostgresResolvedEvidenceSource,
)
from apex.adapters.batch_learning_postgres import PostgresBatchLearningRepository
from apex.adapters.broad_equity_postgres import PostgresBroadEquityRepository
from apex.adapters.candidate_generation_postgres import (
    PostgresCandidateGenerationRepository,
)
from apex.adapters.candidate_generation_tools import (
    LocalMLflowMirrorAdapter,
    OptunaCandidateSearchAdapter,
)
from apex.adapters.coinbase_read_only import (
    NetworkDisabledCoinbasePrivateTransport,
    UnavailableCoinbaseCredentialResolver,
)
from apex.adapters.coinbase_read_only_postgres import PostgresCoinbaseReadOnlyRepository
from apex.adapters.corporate_calibration_depth_postgres import (
    PostgresCorporateCalibrationDepthRepository,
)
from apex.adapters.corporate_fundamentals_postgres import (
    PostgresCorporateFundamentalsRepository,
)
from apex.adapters.corporate_regime_followup_postgres import (
    PostgresCorporateRegimeFollowUpRepository,
)
from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
    _CryptoFollowUpPriceSource,
)
from apex.adapters.counterfactual_ledger_postgres import (
    PostgresCounterfactualLedgerRepository,
)
from apex.adapters.counterfactual_outcome_postgres import (
    PostgresCounterfactualOutcomeRepository,
)
from apex.adapters.cross_market_research_postgres import (
    PostgresCrossMarketResearchRepository,
)
from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
)
from apex.adapters.crypto_market_pulse_postgres import (
    PostgresCryptoMarketPulseRepository,
)
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
from apex.adapters.crypto_stateful_research_postgres import (
    PostgresCryptoStatefulResearchRepository,
)
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.external_paper_fixture import DeterministicPaperBrokerFixture
from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
from apex.adapters.external_strategy_research_postgres import (
    PostgresExternalStrategyDefinitionRepository,
    PostgresExternalStrategyEvaluationRepository,
)
from apex.adapters.first_generated_research_method_sources import (
    PostgresCurrentOpportunityOperationalEvidenceSource,
)
from apex.adapters.genuine_multi_asset_history_postgres import (
    PostgresGenuineMultiAssetHistoryRepository,
)
from apex.adapters.historical_market_data_postgres import (
    PostgresHistoricalMarketDataRepository,
)
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.kalshi_baseline_quote_postgres import PostgresKalshiBaselineQuoteRepository
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.kalshi_read_only import (
    NetworkDisabledKalshiPrivateTransport,
    UnavailableKalshiCredentialResolver,
)
from apex.adapters.kalshi_read_only_postgres import PostgresKalshiReadOnlyRepository
from apex.adapters.kalshi_resolution_postgres import PostgresKalshiResolutionFactRepository
from apex.adapters.live_market_observation_postgres import (
    PostgresLiveMarketObservationRepository,
)
from apex.adapters.live_preflight_postgres import PostgresLivePreflightRepository
from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
from apex.adapters.operating_cycle_postgres import PostgresOperatingCycleRepository
from apex.adapters.operating_cycle_tasks import GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID
from apex.adapters.operational_reliability_postgres import (
    PostgresOperationalReliabilityRepository,
)
from apex.adapters.options_intelligence_postgres import PostgresOptionsIntelligenceRepository
from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_research_operations_postgres import (
    PostgresOptionsResearchOperationsRepository,
)
from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
from apex.adapters.performance_attribution_postgres import (
    PostgresPerformanceAttributionRepository,
)
from apex.adapters.portfolio_intelligence_postgres import PostgresPortfolioIntelligenceRepository
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    postgres_evidence_review_store,
    postgres_operator_console_store,
    postgres_shadow_operations_store,
)
from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
from apex.adapters.research_calibration_postgres import PostgresCalibrationRepository
from apex.adapters.research_calibration_v2_postgres import PostgresCalibrationV2Repository
from apex.adapters.research_evidence_postgres import PostgresResearchEvidenceRepository
from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.adapters.research_integrity_postgres import (
    PostgresResearchIntegrityRepository,
)
from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
from apex.adapters.research_operations_executor import (
    ExistingResearchLedgerExecutor,
    FirstGeneratedOperationalMethodExecutor,
    GovernedResearchExecutor,
    RetainedOutcomeAggregateExperimentExecutor,
)
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
    PostgresRetainedResearchKnowledgeSource,
)
from apex.adapters.research_operations_sources import RetainedResearchAgendaSource
from apex.adapters.research_replication_postgres import PostgresResearchReplicationRepository
from apex.adapters.securities_baseline_simulators import (
    ApexResearchSimulatorAdapter,
    LeanCanonicalSecuritiesAdapter,
    VectorbtSecuritiesAdapter,
)
from apex.adapters.securities_baselines_postgres import (
    PostgresSecuritiesBaselineRepository,
)
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
from apex.adapters.securities_ensemble_postgres import PostgresEnsembleRepository
from apex.adapters.securities_external import LeanSecuritiesEngineAdapter
from apex.adapters.securities_paper_external import LeanSecuritiesPaperAdapter
from apex.adapters.securities_paper_postgres import (
    PostgresSecuritiesPaperCampaignBoundary,
    PostgresSecuritiesPaperRepository,
)
from apex.adapters.securities_postgres import (
    PostgresSecuritiesCampaignBoundary,
    PostgresSecuritiesRepository,
)
from apex.adapters.securities_strategy_intelligence_postgres import (
    PostgresStrategyIntelligenceRepository,
)
from apex.adapters.securities_strategy_lab import StrategyLabSecuritiesSignalBuilder
from apex.adapters.securities_strategy_lab_dataset_postgres import (
    PostgresSecuritiesDirectionDatasetRepository,
)
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository
from apex.adapters.security_reference_postgres import PostgresSecurityReferenceRepository
from apex.adapters.shared_opportunity_postgres import (
    PostgresSharedOpportunityRepository,
)
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.adapters.strategy_evaluation_history_postgres import (
    PostgresInternalStrategyEvaluationRepository,
    PostgresInternalStrategyPromotionRepository,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_event_markets import (
    EventMarketsForwardFeatureAdapter,
    EventMarketsStrategyLabAdapter,
)
from apex.adapters.strategy_lab_postgres import (
    PostgresStrategyLabShadowStore,
    PostgresStrategyLabStore,
)
from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialClosureRepository,
    PostgresStrategyPaperTrialRepository,
)
from apex.adapters.strategy_trial_outcome_assessment_postgres import (
    PostgresStrategyTrialOutcomeAssessmentRepository,
)
from apex.adapters.sustained_shadow_operations_postgres import (
    PostgresSustainedShadowOperationsRepository,
)
from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository
from apex.adapters.total_market_context_postgres import (
    PostgresTotalMarketContextRepository,
)
from apex.application.authorize_order import (
    IdempotencyConflictError,
    MissingPersistenceStateError,
    PersistenceConflictError,
)
from apex.application.autopilot import AutopilotError, CampaignControlError
from apex.application.batch_learning import (
    BatchLearningCycle,
    BatchLearningError,
    BatchLearningLifecycle,
    BatchLearningTriggerType,
    CancelBatchLearningCycle,
    EvaluateBatchLearningTrigger,
)
from apex.application.candidate_generation import (
    CancelAutomatedCandidateSearch,
    CandidateGenerationApplicationError,
    CreateAutomatedCandidateSearchPlan,
    RecoverAutomatedCandidateSearch,
    RunAutomatedCandidateSearch,
    StrategyLabCandidateTrialExecutor,
    create_local_mlflow_workspace,
    reconcile_mlflow_mirror,
)
from apex.application.coinbase_read_only import (
    ApproveCoinbaseReadOnlyInspection,
    InspectCoinbaseReadOnlyCapability,
    coinbase_read_only_workspace,
)
from apex.application.counterfactual_followup import (
    PlanFollowUpObservations,
    plan_current_followup_selection,
)
from apex.application.crypto_paper import InspectCryptoPaperOperatingSeam, paper_workspace
from apex.application.evidence_review import (
    BuildStrategyEvidenceSnapshot,
    BuildStrategyEvidenceSnapshotCommand,
    DecideStrategyEvidenceReview,
    DecideStrategyEvidenceReviewCommand,
    EvidenceReviewConflictError,
    EvidenceReviewError,
    PaperTrialLimits,
    RequestStrategyEvidenceReview,
    RequestStrategyEvidenceReviewCommand,
    StartStrategyEvidenceReview,
    StrategyEvidenceReviewState,
    initial_evidence_review_policies,
)
from apex.application.external_paper import (
    CancelExternalPaperInstruction,
    CreateAuthorizedExternalPaperInstruction,
    ExternalPaperApplicationError,
    FlattenExternalPaperCampaign,
    KillExternalPaper,
    PrepareAuthorizedExternalPaperExecution,
    PrepareAuthorizedExternalPaperReduction,
    ProcessAlpacaPaperFills,
    ProcessExternalPaperFills,
    SubmitAuthorizedPaperInstruction,
    SyncExternalPaperInstruction,
    SynchronizeAlpacaPaperInstruction,
    ValidateAlpacaPaperFill,
    ValidateExternalPaperFill,
    ready_capability,
)
from apex.application.external_strategy_research import (
    external_strategy_detail,
    external_strategy_registry_snapshot,
)
from apex.application.kalshi_operating import kalshi_workspace
from apex.application.kalshi_read_only import (
    ApproveKalshiReadOnlyInspection,
    InspectKalshiReadOnlyCapability,
    kalshi_read_only_workspace,
)
from apex.application.live_market_observation import ObtainContemporaneousMarketObservation
from apex.application.live_preflight import live_preflight_workspace
from apex.application.live_readiness import (
    CredentialsAbsentCapabilityInspector,
    ExportLiveReadinessEvidence,
    InspectProviderCapability,
    MaterializeLiveReadiness,
    RegisterCredentialReference,
    SetExecutionControl,
    live_readiness_workspace,
)
from apex.application.operational_reliability import (
    SupervisedOperatingRunner,
    operational_workspace,
)
from apex.application.operator_console import (
    CreateMoonshotCampaign,
    CreateMoonshotCampaignCommand,
    OperatorActionType,
    OperatorMode,
)
from apex.application.operator_console_evidence import (
    build_followup_progress,
    build_operator_decision_detail,
    build_operator_decisions,
    build_operator_followups,
    build_operator_operations,
    build_operator_options_research,
    build_operator_overview_extensions,
    build_operator_portfolio_mandate,
    enrich_opportunities_with_decisions,
)
from apex.application.operator_console_v1 import (
    OperatorConsoleFilterError,
    build_operator_allocation,
    build_operator_attention,
    build_operator_broad_market,
    build_operator_external_strategy_registry,
    build_operator_home,
    build_operator_learning,
    build_operator_live_preflight,
    build_operator_live_readiness,
    build_operator_opportunities,
    build_operator_paper_operations,
    build_operator_paper_strategy_registry,
    build_operator_research_operations,
)
from apex.application.research_integrity import (
    OpenResearchExperimentFamily,
    ResearchExperimentLifecycle,
    ResearchFamilyState,
    ResearchIntegrityApplicationError,
    ResearchIntegrityService,
    ResearchTrialLedger,
)
from apex.application.research_operations import (
    CancelQueuedResearchExperiment,
    CreateResearchProposal,
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    GenerateResearchProposals,
    ReviewResearchProposal,
    RunGovernedResearchExperiment,
    research_operations_workspace,
    research_review_queue,
)
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    RunSecuritiesConformance,
    RunSecuritiesConformanceCommand,
    RunSecuritiesResearch,
    RunSecuritiesResearchCommand,
    SecuritiesApplicationError,
    SecuritiesIdempotencyConflictError,
    initial_securities_strategy_specifications,
)
from apex.application.securities_baselines import (
    CompareCanonicalSecuritiesSimulators,
    RegisterCanonicalSecuritiesBaselines,
    RunCanonicalSecuritiesBaseline,
    RunCanonicalSecuritiesBaselineCommand,
    SecuritiesBaselineApplicationError,
    SecuritiesBaselineIdempotencyConflict,
    select_current_securities_conformance_reports,
)
from apex.application.securities_paper import (
    ApplySecuritiesCorporateActions,
    BuildSecuritiesPaperConformance,
    BuildSecuritiesPaperOpportunities,
    BuildSecuritiesPaperOpportunityCommand,
    CreateSecuritiesPaperRun,
    CreateSecuritiesPaperRunCommand,
    ExecuteSecuritiesPaperOpportunityCommand,
    ExitSecuritiesPositionCommand,
    ExitSecuritiesPositions,
    MarkSecuritiesPositions,
    RunSecuritiesPaperExecution,
    SecuritiesPaperApplicationError,
    SecuritiesPaperIdempotencyConflictError,
    complete_run,
)
from apex.application.strategy_evaluation_history import internal_strategy_evaluation_registry
from apex.application.strategy_lab import (
    CalibrationMethod,
    ModelFamily,
    StrategyLabError,
    StrategyLabService,
)
from apex.application.strategy_lab_shadow import (
    DeploymentRole,
    ShadowReviewState,
    StrategyLabShadowService,
)
from apex.application.strategy_paper_trial_lifecycle import strategy_paper_trial_lineage
from apex.application.strategy_trial_outcome_assessment import (
    strategy_trial_outcome_report,
    strategy_trial_outcome_summary,
)
from apex.domain.autopilot import EvidenceTier
from apex.domain.candidate_generation import (
    ObjectiveIdentity,
    initial_strategy_lab_search_space,
)
from apex.domain.coinbase_read_only import (
    CoinbaseEndpointClass,
    CoinbaseReadOnlyError,
)
from apex.domain.counterfactual_classification import (
    CURRENT_CLASSIFIER_VERSION,
    KNOWN_CLASSIFIER_VERSIONS,
)
from apex.domain.counterfactual_followup import (
    CURRENT_FOLLOWUP_POLICY_VERSIONS,
    current_followup_evidence_quality_policy_version,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.crypto_paper import estimate_open_position_mark, open_trial_orders_by_strategy
from apex.domain.crypto_universe import CryptoProductObservation
from apex.domain.external_paper import ExternalPaperInstruction
from apex.domain.first_generated_research_method import (
    FirstGeneratedResearchMethodImplementation,
    FirstGeneratedResearchMethodRun,
    FirstGeneratedResearchMethodSelection,
)
from apex.domain.investment_mandate import DEFAULT_MANDATE_LINEAGE_ID
from apex.domain.kalshi_read_only import KalshiEndpointClass, KalshiReadOnlyError
from apex.domain.live_market_observation import (
    ContemporaneousObservationDisposition,
    LiveObservationType,
)
from apex.domain.live_preflight import (
    InternalPreflight,
    LiveIntentEligibilityDecision,
    LiveIntentInvalidation,
    LiveIntentProposal,
    LivePreflightAssessment,
    ProviderPreviewApproval,
    ProviderPreviewAttempt,
    ProviderPreviewCapture,
)
from apex.domain.live_readiness import (
    AccountEnvironment,
    ExecutionControlState,
    LiveReadinessError,
    LiveReadinessInputs,
    LiveRiskPolicy,
    PermissionClass,
    Provider,
    SecretBackend,
)
from apex.domain.operating_cycle import compute_paper_continuation_health
from apex.domain.options_quote_intelligence import IV_RANK_MINIMUM_HISTORY
from apex.domain.research_integrity import ResearchFrequency
from apex.domain.research_operations import (
    PriorResearchKnowledge,
    ResearchAgendaItem,
    ResearchBudgetEvent,
    ResearchExperimentApproval,
    ResearchExperimentQueueEvent,
    ResearchExperimentQueueItem,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalFeasibility,
    ResearchProposalGeneratorRun,
    ResearchProposalOrigin,
    ResearchProposalPriority,
    ResearchProposalReview,
    ResearchProposalState,
    ResearchRecommendation,
    ResearchVenue,
)
from apex.domain.securities import (
    SECURITIES_SHADOW_WARNING,
    SecuritiesSignalDirection,
    SecuritiesStrategyKind,
    SecuritiesStrategySpecification,
)
from apex.domain.securities_baselines import (
    BASELINE_RESEARCH_WARNING,
    CANONICAL_BASELINE_WARNING,
    EXTERNAL_SIMULATOR_WARNING,
    CanonicalSecuritiesSimulationRequest,
    SecuritiesSimulationState,
    SecuritiesSimulatorIdentity,
    build_baseline_scorecard,
    canonical_securities_baselines,
    research_trial_summary,
    scorecard_conformance,
)
from apex.domain.securities_paper import (
    EXTERNAL_ENGINE_WARNING,
    NO_BROKER_WARNING,
    SECURITIES_PAPER_WARNING,
    SecuritiesExecutionPolicy,
    SecuritiesExitReason,
    SecuritiesPaperRunState,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks
from apex.domain.strategy_evaluation_history import (
    InternalStrategyEvaluationRecord,
    evaluate_freshness,
)
from apex.domain.strategy_paper_trial import price_evidence_from_live_observation
from apex.domain.strategy_paper_trial_lifecycle import promoted_trial_slot_version
from apex.domain.strategy_trial_outcome_assessment import StrategyTrialOutcomeAssessment
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    RiskFraction,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

API_SCHEMA_VERSION = "apex.operator.v1"
LOCAL_HOSTS = frozenset(("127.0.0.1", "localhost"))


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    entity_type: str | None = None
    entity_id: str | None = None
    retryable: bool = False
    details: dict[str, str] = Field(default_factory=dict)
    request_id: str


class CampaignCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    initial_capital: str
    maximum_authorized_loss: str
    starts_at: datetime
    expires_at: datetime
    operating_mode: OperatorMode
    maximum_active_risk_fraction: str
    maximum_per_opportunity_risk_fraction: str
    maximum_per_strategy_risk_fraction: str
    maximum_per_correlation_bucket_risk_fraction: str
    maximum_simultaneous_reservations: int = Field(ge=0, le=100)
    maximum_simultaneous_positions: int = Field(ge=0, le=100)
    maximum_capital_lockup_seconds: int = Field(ge=1, le=86_400)
    minimum_conservative_expected_value: str
    minimum_evidence_tier: EvidenceTier
    allow_profit_compounding: bool = False
    operator_note: str = Field(default="", max_length=500)

    @field_validator(
        "initial_capital",
        "maximum_authorized_loss",
        "maximum_active_risk_fraction",
        "maximum_per_opportunity_risk_fraction",
        "maximum_per_strategy_risk_fraction",
        "maximum_per_correlation_bucket_risk_fraction",
        "minimum_conservative_expected_value",
    )
    @classmethod
    def canonical_decimal(cls, value: str) -> str:
        return str(parse_decimal(value))


class StrategyLabDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    adapter: str = Field(pattern="^event-market-v1$")
    name: str = Field(min_length=1, max_length=240)
    cutoff_at: datetime
    strategy_id: str | None = Field(default=None, min_length=1, max_length=160)
    strategy_version: str | None = Field(default=None, min_length=1, max_length=160)
    configuration_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class StrategyLabTrainingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=160)
    model_family: ModelFamily
    calibration: CalibrationMethod
    seed: int = Field(ge=0, le=2_147_483_647)
    hyperparameters: dict[str, str | int | float] = Field(default_factory=dict)


class CandidateSearchPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    experiment_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=240)
    dataset_id: str = Field(min_length=1, max_length=160)
    feature_schema_id: str = Field(min_length=1, max_length=160)
    specification: str = Field(pattern="^strategy-lab-initial-bounded-v1$")
    objective: ObjectiveIdentity
    maximum_trials: int = Field(ge=1, le=500)
    seed: int = Field(ge=0, le=2_147_483_647)
    operator_reason: str = Field(min_length=1, max_length=500)


class CandidateSearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_trials: int = Field(ge=1, le=500)


class BatchLearningPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=240)
    source_scope: str = Field(pattern="^strategy-lab-forward-evaluations-v1$")
    feature_schema_id: str = Field(min_length=1, max_length=160)
    search_template: str = Field(pattern="^strategy-lab-initial-bounded-v1$")
    minimum_new_groups: int = Field(ge=1, le=10_000)
    minimum_new_observations: int = Field(ge=1, le=100_000)
    maximum_trials_per_cycle: int = Field(ge=1, le=500)
    maximum_cycles: int = Field(ge=1, le=1_000)
    reason: str = Field(min_length=1, max_length=500)


class BatchLearningActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    confirm_plan_id: str = Field(min_length=1, max_length=160)
    confirm_plan_hash: str = Field(pattern="^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)


class BatchLearningReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=500)


class BatchLearningAttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    deployment_id: str = Field(min_length=1, max_length=160)


class BatchLearningCutoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cutoff_at: datetime


class StrategyLabShadowReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=500)


class StrategyLabShadowDecisionRequest(StrategyLabShadowReviewRequest):
    note: str = Field(default="", max_length=500)


class StrategyLabShadowApprovalRequest(StrategyLabShadowReviewRequest):
    role: DeploymentRole
    confirm_candidate: str = Field(min_length=1, max_length=160)
    confirm_dataset: str = Field(min_length=1, max_length=160)
    confirm_artifact_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    confirm_evaluation: str = Field(min_length=1, max_length=160)


class StrategyLabShadowBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    adapter: str = Field(pattern="^event-market-retained-v1$")
    cutoff_at: datetime
    maximum_rows: int = Field(ge=1, le=10_000)


class StrategyLabShadowCutoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cutoff_at: datetime


class StrategyLabChampionRequest(StrategyLabShadowReviewRequest):
    confirm_deployment: str = Field(min_length=1, max_length=160)


class SecuritiesDataBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    managed_source: str = Field(min_length=6, max_length=500)
    dataset_name: str = Field(min_length=1, max_length=160)

    @field_validator("managed_source")
    @classmethod
    def safe_managed_source(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or not value.endswith((".jsonl", ".csv"))
        ):
            raise ValueError("managed_source must be a safe managed JSONL or CSV identifier")
        return value


class SecuritiesResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    campaign_id: str = Field(min_length=1, max_length=160)
    strategy_id: str = Field(min_length=1, max_length=160)
    data_batch_id: str = Field(min_length=1, max_length=160)
    universe: list[str] = Field(min_length=1, max_length=500)
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)
    decision_cutoff: datetime
    benchmark_instrument_id: str = Field(min_length=1, max_length=160)

    @field_validator("start", "end")
    @classmethod
    def iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class SecuritiesStrategyLabResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    campaign_id: str = Field(min_length=1, max_length=160)
    deployment_id: str = Field(min_length=1, max_length=160)
    data_batch_id: str = Field(min_length=1, max_length=160)
    universe: list[str] = Field(min_length=1, max_length=500)
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)
    decision_cutoff: datetime
    benchmark_instrument_id: str = Field(min_length=1, max_length=160)

    @field_validator("start", "end")
    @classmethod
    def iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class SecuritiesConformanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    research_run_id: str = Field(min_length=1, max_length=160)
    external_engine: str = Field(pattern="^LEAN$")


class SecuritiesBaselineSuiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    campaign_id: str = Field(min_length=1, max_length=160)
    data_batch_id: str = Field(min_length=1, max_length=160)
    universe: list[str] = Field(min_length=1, max_length=500)
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)
    decision_cutoff: datetime
    simulators: list[SecuritiesSimulatorIdentity] = Field(min_length=1, max_length=3)

    @field_validator("start", "end")
    @classmethod
    def baseline_iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator("simulators")
    @classmethod
    def research_only_simulators(
        cls, value: list[SecuritiesSimulatorIdentity]
    ) -> list[SecuritiesSimulatorIdentity]:
        allowed = {
            SecuritiesSimulatorIdentity.APEX_RESEARCH,
            SecuritiesSimulatorIdentity.VECTORBT,
            SecuritiesSimulatorIdentity.LEAN,
        }
        if any(item not in allowed for item in value):
            raise ValueError("baseline suite accepts research simulators only")
        if len(value) != len(set(value)):
            raise ValueError("baseline suite simulators must be unique")
        return value


class ResearchExperimentFamilyRequest(BaseModel):
    """One managed, bounded Securities research scope; no executable input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=240)
    vertical: str = Field(pattern="^SECURITIES$")
    research_question: str = Field(min_length=1, max_length=2_000)
    target_label_identity: str = Field(min_length=1, max_length=240)
    benchmark_identities: list[str] = Field(min_length=1, max_length=20)
    data_batch_id: str = Field(min_length=1, max_length=160)
    universe_id: str = Field(min_length=1, max_length=160)
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)
    permitted_families: list[str] = Field(min_length=1, max_length=20)
    managed_specification: str = Field(pattern="^canonical-securities-baselines-v1$")
    maximum_trials: int = Field(ge=3, le=500)
    maximum_feature_variants: int = Field(ge=1, le=500)
    maximum_model_variants: int = Field(ge=1, le=500)
    maximum_simulator_variants: int = Field(ge=1, le=500)
    operator_reason: str = Field(min_length=1, max_length=500)

    @field_validator("start", "end")
    @classmethod
    def research_iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class ResearchExperimentTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operator_reason: str = Field(min_length=1, max_length=500)


class ResearchOverfittingEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cutoff_at: datetime


class GovernedResearchProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    proposal_id: str = Field(min_length=1, max_length=300)
    version: int = Field(ge=1, le=100)
    agenda_item_id: str = Field(min_length=1, max_length=300)
    venue: str = Field(pattern="^(CRYPTO|KALSHI|CROSS_VENUE)$")
    hypothesis: str = Field(min_length=1, max_length=2_000)
    expected_mechanism: str = Field(min_length=1, max_length=2_000)
    instruments: list[str] = Field(min_length=1, max_length=50)
    proposed_family: str = Field(min_length=1, max_length=300)
    definition_kind: str = Field(min_length=1, max_length=100)
    strategy_definition: list[tuple[str, str]] = Field(min_length=1, max_length=100)
    exact_change_from_prior: str = Field(min_length=1, max_length=2_000)
    prior_research_ids: list[str] = Field(default_factory=list, max_length=100)
    required_data_ids: list[str] = Field(min_length=1, max_length=100)
    experiment_configuration: list[tuple[str, str]] = Field(min_length=1, max_length=100)
    validation_windows: list[tuple[str, str]] = Field(min_length=1, max_length=20)
    cost_assumptions: list[tuple[str, str]] = Field(min_length=1, max_length=50)
    success_criteria: list[str] = Field(min_length=1, max_length=50)
    failure_criteria: list[str] = Field(min_length=1, max_length=50)
    maximum_experiment_runs: int = Field(ge=1, le=12)
    origin: str = Field(pattern="^(OPERATOR|SYSTEM_HEURISTIC|AI_ASSISTED|IMPORTED)$")
    actor: str = Field(min_length=1, max_length=300)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class GovernedResearchReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_state: str = Field(
        pattern="^(READY_FOR_REVIEW|APPROVED_FOR_EXPERIMENT|REJECTED|DEFERRED|DUPLICATE|INVALID)$"
    )
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=1_000)
    confirmation: str = Field(min_length=1, max_length=500)


class GovernedResearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str = Field(min_length=1, max_length=300)
    actor: str = Field(min_length=1, max_length=300)
    confirmation: str = Field(min_length=1, max_length=500)


class ResearchProposalGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    generator_run_id: str = Field(min_length=1, max_length=300)
    maximum_proposals: int = Field(default=3, ge=1, le=3)
    origin: str = Field(default="SYSTEM_HEURISTIC", pattern="^(SYSTEM_HEURISTIC|AI_ASSISTED)$")
    actor: str = Field(min_length=1, max_length=300)


class ResearchExperimentEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    queue_id: str = Field(min_length=1, max_length=300)
    proposal_id: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=300)
    actor: str = Field(min_length=1, max_length=300)


class ResearchExperimentQueueActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=1_000)


class SecuritiesPaperRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    campaign_id: str = Field(min_length=1, max_length=160)
    trial_id: str = Field(min_length=1, max_length=160)
    strategy_id: str = Field(min_length=1, max_length=160)
    data_batch_id: str = Field(min_length=1, max_length=160)
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)

    @field_validator("start", "end")
    @classmethod
    def iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class SecuritiesPaperAsOfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime


class SecuritiesPaperExitRequest(SecuritiesPaperAsOfRequest):
    reason: SecuritiesExitReason


class SecuritiesPaperConformanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    paper_run_id: str = Field(min_length=1, max_length=160)
    external_engine: str = Field(pattern="^LEAN$")


class CycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: OperatorMode
    maximum_strategies: int = Field(default=25, ge=0, le=100)
    maximum_proposals: int = Field(default=100, ge=0, le=1000)
    operator_note: str = Field(default="", max_length=500)


class LiveShadowSessionRequest(BaseModel):
    """Bounded foreground public-observation input; it never conveys trade terms."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    iterations: int = Field(default=3, ge=1, le=20)
    interval_seconds: int = Field(default=30, ge=10, le=3_600)
    market_limit: int = Field(default=20, ge=1, le=100)
    maximum_pages: int = Field(default=1, ge=1, le=3)
    maximum_enrichments: int = Field(default=20, ge=1, le=100)
    observations_per_market: int = Field(default=3, ge=2, le=20)
    orderbook_depth: int = Field(default=5, ge=1, le=20)


class MultiStrategyShadowSessionRequest(BaseModel):
    """Bounded shared public evidence collection with no financial authority."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    iterations: int = Field(default=4, ge=3, le=20)
    interval_seconds: int = Field(default=30, ge=10, le=3_600)
    market_limit: int = Field(default=20, ge=3, le=50)
    maximum_pages: int = Field(default=3, ge=1, le=5)
    maximum_enrichments: int = Field(default=40, ge=3, le=80)
    orderbook_depth: int = Field(default=5, ge=1, le=20)


class ShadowEvidencePlanRequest(BaseModel):
    """Finite public-evidence plan terms; no order or execution term exists here."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    iterations: int = Field(default=12, ge=3, le=288)
    interval_seconds: int = Field(default=300, ge=30, le=86_400)
    market_count: int = Field(default=10, ge=3, le=50)
    maximum_pages: int = Field(default=3, ge=1, le=5)
    maximum_enrichments: int = Field(default=30, ge=3, le=80)
    maximum_markets_per_event: int = Field(default=2, ge=1, le=5)
    maximum_markets_per_series: int = Field(default=4, ge=1, le=10)
    orderbook_depth: int = Field(default=5, ge=1, le=20)
    maximum_predictions_per_iteration: int = Field(default=100, ge=1, le=500)
    maximum_opportunities_per_iteration: int = Field(default=40, ge=1, le=500)
    market_tickers: list[str] | None = Field(default=None, min_length=3, max_length=50)


class AtomicShadowEvidencePlanRequest(BaseModel):
    """One bounded discover/enrich/freeze request with no trading terms."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    window_start: datetime
    window_end: datetime
    iterations: int = Field(ge=3, le=288)
    interval_seconds: int = Field(ge=30, le=86_400)
    target_markets: int = Field(ge=3, le=50)
    minimum_markets: int = Field(ge=3, le=50)
    minimum_events: int = Field(ge=3, le=50)
    maximum_pages: int = Field(ge=1, le=5)
    maximum_inspected_markets: int = Field(ge=20, le=500)
    maximum_enrichments: int = Field(ge=3, le=80)
    maximum_markets_per_event: int = Field(ge=1, le=5)
    maximum_markets_per_series: int = Field(ge=1, le=10)
    orderbook_depth: int = Field(ge=1, le=20)
    maximum_orderbook_preflights: int = Field(default=80, ge=3, le=80)
    microstructure_configuration_version: str = Field(
        default="orderbook-microstructure-shadow-v1", min_length=1, max_length=160
    )
    exclude_plan_ids: list[str] = Field(default_factory=list, max_length=20)
    exclude_prior_plan_markets: bool = True
    exclude_prior_plan_events: bool = False
    exclude_active_plan_markets: bool = True


class ShadowOutcomeCollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    maximum_markets: int = Field(default=100, ge=1, le=500)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operator_reason: str = Field(min_length=1, max_length=500)
    confirmation: str | None = Field(default=None, max_length=160)


class SustainedShadowPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operator_reason: str = Field(min_length=1, max_length=500)


class SustainedShadowConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    confirm_plan_id: str = Field(min_length=1, max_length=160)


class MarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    orderbook_run_id: str = Field(min_length=1, max_length=160)
    operator_note: str = Field(default="", max_length=500)


class SettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    settlement_fact_id: str = Field(min_length=1, max_length=160)
    operator_note: str = Field(default="", max_length=500)


class OperatorThesisRequest(BaseModel):
    """Only forward probabilities and untrusted operator narrative are accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    market_snapshot_id: str = Field(min_length=1, max_length=160)
    point_probabilities: dict[str, str] = Field(min_length=2, max_length=32)
    conservative_probabilities: dict[str, str] = Field(min_length=2, max_length=32)
    resolution_at: datetime
    requested_risk_fraction: str
    thesis_summary: str = Field(min_length=1, max_length=4_000)
    evidence_references: list[str] = Field(min_length=1, max_length=32)
    correlation_buckets: list[str] = Field(min_length=1, max_length=8)
    operator_note: str = Field(default="", max_length=500)

    @field_validator("requested_risk_fraction")
    @classmethod
    def canonical_fraction(cls, value: str) -> str:
        return str(parse_decimal(value))

    @field_validator("point_probabilities", "conservative_probabilities")
    @classmethod
    def canonical_probability_values(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: str(parse_decimal(item)) for key, item in value.items()}


class PredictionEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    settlement_fact_id: str = Field(min_length=1, max_length=160)
    operator_note: str = Field(default="", max_length=500)


class StrategyAuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    requested_authority: str = Field(min_length=1, max_length=20)
    operator_reason: str = Field(min_length=1, max_length=500)
    evidence_review_id: str = Field(min_length=1, max_length=160)
    confirmation_text: str | None = Field(default=None, max_length=160)


class ExternalPaperKillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=240)


class ExternalPaperFlattenRequest(ExternalPaperKillRequest):
    pass


class ExternalPaperInstructionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    authorization_id: str = Field(min_length=1, max_length=160)
    symbol: str = Field(min_length=1, max_length=16)
    side: str = Field(min_length=3, max_length=4)
    quantity: str = Field(min_length=1, max_length=40)
    maximum_notional: str = Field(min_length=1, max_length=40)
    expires_at: datetime


class ExternalPaperReductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    position_id: str = Field(min_length=1, max_length=160)
    decision_at: datetime
    earliest_fill_at: datetime
    expires_at: datetime


class ExternalPaperFillValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1, max_length=160)


class ExternalPaperPreparationRequest(BaseModel):
    """Exact retained IDs only; broker input is deliberately not accepted."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    binding_id: str = Field(min_length=1, max_length=160)


class EvidenceSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    policy_id: str = Field(min_length=1, max_length=160)
    configuration_hash: str = Field(min_length=64, max_length=64)
    policy_version: str = Field(
        default="apex-evidence-review-policy-v1", min_length=1, max_length=160
    )
    cutoff_at: datetime
    full_history_started_at: datetime
    recent_history_started_at: datetime


class EvidenceReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    snapshot_id: str = Field(min_length=1, max_length=160)
    campaign_id: str = Field(min_length=1, max_length=160)
    expires_at: datetime
    operator_reason: str = Field(min_length=1, max_length=500)
    operator_note: str = Field(default="", max_length=500)


class EvidenceReviewTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operator_reason: str = Field(min_length=1, max_length=500)
    operator_note: str = Field(default="", max_length=500)


class PaperTrialApprovalRequest(EvidenceReviewTransitionRequest):
    paper_execution_policy_id: str = Field(min_length=1, max_length=160)
    paper_execution_policy_hash: str = Field(min_length=64, max_length=64)
    starts_at: datetime
    expires_at: datetime
    maximum_paper_cycles: int = Field(ge=1, le=10_000)
    maximum_approved_opportunities: int = Field(ge=1, le=10_000)
    maximum_paper_orders: int = Field(ge=1, le=10_000)
    maximum_simultaneous_open_positions: int = Field(ge=1, le=100)
    maximum_per_opportunity_risk_fraction: str
    maximum_strategy_active_risk_fraction: str
    maximum_cumulative_authorized_risk: str
    maximum_cumulative_realized_paper_loss: str
    maximum_paper_drawdown: str
    maximum_capital_lockup_seconds: int = Field(ge=1, le=86_400)
    allowed_instrument_class: str = Field(min_length=1, max_length=80)
    allowed_provider: str = Field(min_length=1, max_length=80)
    confirmation_text: str = Field(min_length=1, max_length=500)

    @field_validator(
        "maximum_per_opportunity_risk_fraction",
        "maximum_strategy_active_risk_fraction",
        "maximum_cumulative_authorized_risk",
        "maximum_cumulative_realized_paper_loss",
        "maximum_paper_drawdown",
    )
    @classmethod
    def canonical_trial_decimal(cls, value: str) -> str:
        return str(parse_decimal(value))


class CredentialReferenceRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reference_id: str = Field(min_length=1, max_length=300)
    version: int = Field(ge=1)
    provider: str = Field(pattern="^(COINBASE|KALSHI|ALPACA)$")
    account_environment: str = Field(pattern="^(PAPER|LIVE)$")
    account_purpose: str = Field(min_length=1, max_length=300)
    required_permissions: tuple[str, ...]
    secret_backend: str = Field(
        pattern="^(ENVIRONMENT_REFERENCE|LOCAL_IGNORED_FILE_REFERENCE|OPERATING_SYSTEM_SECRET_REFERENCE)$"
    )
    backend_reference_id: str = Field(min_length=1, max_length=300)
    previous_reference_id: str | None = Field(default=None, max_length=300)
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class ProviderCapabilityInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    snapshot_id: str = Field(min_length=1, max_length=300)
    provider: str = Field(pattern="^(COINBASE|KALSHI|ALPACA)$")
    account_scope_id: str = Field(min_length=1, max_length=300)
    account_environment: str = Field(pattern="^(PAPER|LIVE)$")
    credential_reference_id: str | None = Field(default=None, max_length=300)
    request_limit: int = Field(default=0, ge=0, le=5)
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class CoinbaseReadApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_id: str = Field(min_length=1, max_length=300)
    credential_reference_id: str = Field(min_length=1, max_length=300)
    account_purpose: str = Field(min_length=1, max_length=300)
    allowed_endpoint_classes: tuple[str, ...] = Field(min_length=2, max_length=5)
    maximum_request_count: int = Field(ge=1, le=10)
    timeout_seconds: int = Field(ge=1, le=30)
    expires_at: datetime
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class CoinbaseReadOnlyInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    attempt_id: str = Field(min_length=1, max_length=300)
    capture_id: str = Field(min_length=1, max_length=300)
    readiness_snapshot_id: str = Field(min_length=1, max_length=300)
    approval_id: str | None = Field(default=None, max_length=300)
    credential_reference_id: str | None = Field(default=None, max_length=300)
    account_purpose: str = Field(min_length=1, max_length=300)
    product_ids: tuple[str, ...] = Field(default=(), max_length=5)
    control_id: str = Field(min_length=1, max_length=300)


class KalshiReadApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_id: str = Field(min_length=1, max_length=300)
    credential_reference_id: str = Field(min_length=1, max_length=300)
    account_purpose: str = Field(min_length=1, max_length=300)
    allowed_endpoint_classes: tuple[str, ...] = Field(min_length=3, max_length=4)
    maximum_request_count: int = Field(ge=3, le=4)
    timeout_seconds: int = Field(ge=1, le=30)
    expires_at: datetime
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class KalshiReadOnlyInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    attempt_id: str = Field(min_length=1, max_length=300)
    capture_id: str = Field(min_length=1, max_length=300)
    readiness_snapshot_id: str = Field(min_length=1, max_length=300)
    approval_id: str | None = Field(default=None, max_length=300)
    credential_reference_id: str | None = Field(default=None, max_length=300)
    account_purpose: str = Field(min_length=1, max_length=300)
    control_id: str = Field(min_length=1, max_length=300)


class ExecutionControlTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=300)
    state: str = Field(pattern="^(DISABLED|PAPER_ONLY|LIVE_BLOCKED|EMERGENCY_STOP)$")
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class LiveReadinessMaterializationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assessment_id: str = Field(min_length=1, max_length=300)
    decision_id: str = Field(min_length=1, max_length=300)
    policy_id: str = Field(min_length=1, max_length=300)
    provider: str = Field(pattern="^(COINBASE|KALSHI|ALPACA)$")
    account_scope_id: str = Field(min_length=1, max_length=300)
    control_id: str = Field(min_length=1, max_length=300)
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class LiveReadinessExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    export_id: str = Field(min_length=1, max_length=300)
    assessment_id: str = Field(min_length=1, max_length=300)
    actor: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)
    confirmation: str = Field(min_length=1, max_length=700)


class PaperActivationTransitionRequest(BaseModel):
    """Governed request to enable or disable internal PAPER consideration on
    one exact retained OperatingCyclePlan.  Entirely separate from LIVE
    authority; grants no provider-write permission."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=300)
    state: str = Field(pattern="^(ENABLED|DISABLED)$")
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=300)


def _decimal(value: Decimal) -> str:
    normalized = value.normalize() if value != 0 else Decimal(0)
    return format(normalized, "f")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))  # type: ignore[arg-type]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _envelope(value: object, *, capability: str = "SIMULATION_PAPER_ONLY") -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "capability": capability,
        "data": _jsonable(value),
    }


def _securities_envelope(value: object) -> dict[str, object]:
    return _envelope(value, capability="SECURITIES_SHADOW_ONLY")


def _securities_paper_envelope(value: object) -> dict[str, object]:
    return _envelope(value, capability="SECURITIES_PAPER_SIMULATION_ONLY")


def _crypto_paper_envelope(value: object) -> dict[str, object]:
    return _envelope(value, capability="CRYPTO_SPOT_PAPER_SIMULATION_ONLY")


def _kalshi_paper_envelope(value: object) -> dict[str, object]:
    return _envelope(value, capability="KALSHI_PAPER_SIMULATION_ONLY")


def _stable_linkage_counts(security_identity_repository: Any) -> dict[str, int]:
    """Linked/unlinked/ambiguous counts for the latest options stable-
    linkage run only -- bounded by that one run's distinct-raw-symbol size
    (thousands), never the underlying retained contract count (millions).
    """
    run_id = security_identity_repository.latest_option_linkage_run_id()
    linkages = (
        security_identity_repository.option_linkages_for_run(run_id) if run_id is not None else ()
    )
    return {
        "linked": sum(1 for item in linkages if item.outcome.value == "LINKED"),
        "unlinked": sum(1 for item in linkages if item.outcome.value == "UNLINKED"),
        "ambiguous": sum(1 for item in linkages if item.outcome.value == "AMBIGUOUS"),
    }


def _corporate_regime_followup_summary(repository: Any) -> dict[str, object]:
    """Corporate Calibration-Qualified Regime Follow-Up v1's read-only
    projection: parent lineage, calibration lineage, regime split, null
    calibration, stability, incremental contribution, conclusion, evidence
    grade, and independent-confirmation requirement -- reusing exactly the
    retained ``CorporateRegimeFollowUpSpecification``/``...Result`` shapes,
    never a separately maintained summary fact."""

    from apex.application.corporate_regime_followup import PARENT_FOLLOW_UP_ID

    specification = repository.specification_by_follow_up(PARENT_FOLLOW_UP_ID)
    result = (
        repository.result_by_specification(specification.specification_id)
        if specification is not None
        else None
    )
    control_count = (
        len(repository.controls_for_specification(specification.specification_id))
        if specification is not None
        else 0
    )
    return {
        "follow_up_id": PARENT_FOLLOW_UP_ID,
        "specification": specification,
        "result": result,
        "null_control_count": control_count,
        "empty_state": (
            "NO_RETAINED_REGIME_FOLLOWUP_SPECIFICATION" if specification is None else None
        ),
        "authority": "NONE",
    }


def _options_quote_chain_summary(options_quote_intelligence_repository: Any) -> dict[str, object]:
    """Genuine current option-chain quote/volatility intelligence, bounded
    to the latest declared plan's cohort. Never generates a hypothesis,
    never implies edge, and carries ``authority = NONE`` throughout --
    identical semantics to the reference-only summary above."""

    capability = options_quote_intelligence_repository.latest_capability(
        "ALPACA_OPTIONS_MARKET_DATA"
    )
    plan = options_quote_intelligence_repository.latest_plan()
    runs = (
        options_quote_intelligence_repository.runs_for_plan(plan.plan_id)
        if plan is not None
        else ()
    )
    latest_run = runs[-1] if runs else None
    underlyings: list[dict[str, object]] = []
    for member in plan.members if plan is not None else ():
        snapshot = options_quote_intelligence_repository.latest_snapshot_for_underlying(
            member.stable_security_id
        )
        if snapshot is None:
            underlyings.append(
                {
                    "symbol": member.symbol,
                    "underlying_canonical_security_id": member.stable_security_id,
                    "snapshot_id": None,
                    "data_quality_status": "NO_RETAINED_SNAPSHOT",
                }
            )
            continue
        features = options_quote_intelligence_repository.research_features_by_snapshot(
            snapshot.snapshot_id
        )
        underlyings.append(
            {
                "symbol": member.symbol,
                "underlying_canonical_security_id": member.stable_security_id,
                "snapshot_id": snapshot.snapshot_id,
                "captured_at": snapshot.captured_at,
                "data_quality_status": snapshot.data_quality_status.value,
                "contracts_considered": snapshot.contracts_considered,
                "contracts_quoted": snapshot.contracts_quoted,
                "atm_iv": features.atm_iv_nearest_expiration if features else None,
                "skew_target_delta": (
                    features.skew_target_delta_nearest_expiration if features else None
                ),
                "term_structure_near_far_spread": (
                    features.term_structure_near_far_spread if features else None
                ),
                "expected_move_percent_straddle": (
                    features.expected_move_percent_straddle if features else None
                ),
                "expected_move_percent_iv_derived": (
                    features.expected_move_percent_iv_derived if features else None
                ),
                "iv_vs_realized_premium_ratio": (
                    features.iv_vs_realized_premium_ratio if features else None
                ),
                "liquidity_state": features.liquidity_state if features else None,
                "authority": snapshot.authority,
            }
        )
    return {
        "contract": "OPTIONS_CHAIN_SNAPSHOT_V1",
        "provider": "ALPACA_OPTIONS_MARKET_DATA",
        "capability": capability,
        "plan": (
            {
                "plan_id": plan.plan_id,
                "cohort_policy_id": plan.cohort_policy_id,
                "member_count": len(plan.members),
                "declared_symbols": tuple(item.symbol for item in plan.members),
                "unresolved_symbols": plan.unresolved_symbols,
                "bounds": plan.bounds,
                "provider": plan.provider,
                "feed": plan.feed,
                "created_at": plan.created_at,
            }
            if plan is not None
            else None
        ),
        "latest_run": (
            {
                "run_id": latest_run.run_id,
                "status": latest_run.status.value,
                "requested_at": latest_run.requested_at,
                "completed_at": latest_run.completed_at,
                "underlyings_requested": latest_run.underlyings_requested,
                "underlyings_captured": latest_run.underlyings_captured,
                "provider_request_count": latest_run.provider_request_count,
                "payload_bytes": latest_run.payload_bytes,
                "runtime_seconds": latest_run.runtime_seconds,
                "reason": latest_run.reason,
            }
            if latest_run is not None
            else None
        ),
        "underlyings": tuple(underlyings),
        "historical_options_pricing_note": "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",
        "iv_history_note": "IV_HISTORY_INSUFFICIENT until 20+ snapshots retained per underlying",
        "validation_report_command": "options-quote-validation-report",
        "warnings": (
            "OPTIONS QUOTE INTELLIGENCE IS RESEARCH ONLY",
            "MIDPOINT IS DESCRIPTIVE, NEVER EXECUTABLE",
            "EXPECTED MOVE IS NOT A CONFIDENCE INTERVAL",
            "SKEW IS MARKET PRICING EVIDENCE, NOT A DIRECTIONAL SIGNAL",
            "IV-VERSUS-REALIZED PREMIUM IS NOT AN AUTOMATIC EDGE",
            "NO OPTION ORDER AUTHORITY IS ENABLED",
        ),
        "authority": "NONE",
    }


def _options_structures_summary(
    options_structures_repository: Any, options_quote_intelligence_repository: Any
) -> dict[str, object]:
    """Genuine defined-risk options structure economics, bounded to the
    latest declared structure-research policy and the latest quote-chain
    plan's cohort. Extends the existing options-intelligence workspace view
    rather than creating a second dashboard. Never generates a hypothesis,
    never implies edge, and carries ``authority = NONE`` throughout --
    identical semantics to the quote-chain summary above."""

    from apex.domain.options_structures import (
        STRUCTURE_ECONOMICS_WARNINGS,
        STRUCTURE_RESEARCH_LIMITATIONS,
    )

    policy = options_structures_repository.latest_structure_policy()
    plan = options_quote_intelligence_repository.latest_plan()
    underlyings: list[dict[str, object]] = []
    total_structures_retained = 0
    if policy is not None and plan is not None:
        for member in plan.members:
            research_snapshot = (
                options_structures_repository.latest_research_snapshot_for_underlying(
                    member.stable_security_id
                )
            )
            if research_snapshot is None:
                underlyings.append(
                    {
                        "symbol": member.symbol,
                        "underlying_canonical_security_id": member.stable_security_id,
                        "research_snapshot_id": None,
                        "structures_retained": 0,
                    }
                )
                continue
            structures = options_structures_repository.structures_by_ids(
                research_snapshot.retained_structure_ids
            )
            total_structures_retained += len(structures)
            underlyings.append(
                {
                    "symbol": member.symbol,
                    "underlying_canonical_security_id": member.stable_security_id,
                    "research_snapshot_id": research_snapshot.research_snapshot_id,
                    "source_chain_snapshot_id": research_snapshot.source_chain_snapshot_id,
                    "evaluated_at": research_snapshot.evaluated_at,
                    "generation_report": research_snapshot.generation_report,
                    "structures_retained": len(structures),
                    # Bounded per-underlying sample -- the full retained set
                    # is reachable via options-structures-snapshot-inspect.
                    "structures": tuple(
                        {
                            "structure_id": item.structure_id,
                            "structure_type": item.structure_type,
                            "expiration": item.expiration,
                            "dte": item.dte,
                            "long_strike": item.long_leg.strike,
                            "short_strike": item.short_leg.strike,
                            "width": item.width,
                            "capital_required": item.economics.capital_required,
                            "maximum_loss": item.economics.maximum_loss,
                            "maximum_profit": item.economics.maximum_profit,
                            "breakeven": item.economics.breakeven,
                            "reward_risk_ratio": item.economics.reward_risk_ratio,
                            "net_delta": item.greeks.net_delta,
                            "net_theta": item.greeks.net_theta,
                            "net_vega": item.greeks.net_vega,
                            "quality_label": item.quality.label,
                            "expected_value_state": item.expected_value_state,
                        }
                        for item in structures[:10]
                    ),
                }
            )
    return {
        "contract": "OPTION_STRUCTURE_CANDIDATE_V1",
        "policy": policy,
        "underlyings": tuple(underlyings),
        "total_structures_retained": total_structures_retained,
        "structure_policy_create_command": "options-structure-policy-create",
        "structures_build_command": "options-structures-build",
        "structure_comparison_command": "options-structure-comparison",
        "limitations": STRUCTURE_RESEARCH_LIMITATIONS,
        "warnings": STRUCTURE_ECONOMICS_WARNINGS,
        "authority": "NONE",
    }


def _options_research_operations_summary(
    options_research_operations_repository: Any, options_quote_intelligence_repository: Any
) -> dict[str, object]:
    """Fresh Options Evidence Operations and Structure Research Baseline v1
    -- genuine due state, capture window, latest retained session/
    checkpoint, and per-underlying longitudinal readiness. Extends the
    existing options-intelligence workspace view rather than creating a
    second dashboard; carries ``authority = NONE`` throughout, and reads
    only already-retained evidence plus the reviewed exchange calendar --
    never a provider request."""

    from datetime import UTC, datetime

    from apex.adapters.options_research_capture import resolve_options_capture_due_decision
    from apex.application.options_research_operations import ComputeOptionsHistoryReadiness
    from apex.domain.options_research_operations import (
        MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
        OPTIONS_RESEARCH_OPERATIONS_WARNINGS,
        ResearchReadinessState,
    )

    now = datetime.now(tz=UTC)
    plan, window_policy, sessions, decision = resolve_options_capture_due_decision(
        quote_repository=options_quote_intelligence_repository,
        checkpoint_repository=options_research_operations_repository,
        as_of=now,
    )
    latest_checkpoint = (
        options_research_operations_repository.latest_checkpoint(plan.plan_id)
        if plan is not None
        else None
    )
    readiness_service = ComputeOptionsHistoryReadiness(
        quote_repository=options_quote_intelligence_repository
    )

    def _readiness_row(member: Any) -> dict[str, object]:
        readiness = readiness_service.execute(
            underlying_canonical_security_id=member.stable_security_id,
            sessions=sessions,
            policy=window_policy,
            minimum_required_sessions=MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
        )
        return {
            "symbol": member.symbol,
            "underlying_canonical_security_id": member.stable_security_id,
            "valid_snapshot_count": readiness.valid_snapshot_count,
            "distinct_session_count": readiness.distinct_session_count,
            "first_session_id": readiness.first_session_id,
            "latest_session_id": readiness.latest_session_id,
            "minimum_required_sessions": readiness.minimum_required_sessions,
            "state": readiness.state,
            "reasons": readiness.reasons,
        }

    readiness_rows = tuple(
        _readiness_row(member) for member in (plan.members if plan is not None else ())
    )
    longitudinal_ready_count = sum(
        1
        for row in readiness_rows
        if row["state"] is ResearchReadinessState.LONGITUDINAL_RESEARCH_READY
    )
    return {
        "contract": "OPTIONS_FORWARD_EVIDENCE_CHECKPOINT_V1",
        "plan_id": None if plan is None else plan.plan_id,
        "capture_due": {
            "status": decision.status,
            "session_id": decision.session_id,
            "window_open_at": decision.window_open_at,
            "window_close_at": decision.window_close_at,
            "reasons": decision.reasons,
        },
        "capture_window_policy": window_policy,
        "latest_checkpoint": latest_checkpoint,
        "per_underlying_history_readiness": readiness_rows,
        "longitudinal_research_ready_underlying_count": longitudinal_ready_count,
        "minimum_distinct_sessions_for_longitudinal_research": (
            MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH
        ),
        "operator_commands": {
            "due_inspect": "options-research-due-inspect",
            "run_once": "options-research-run-once",
            "checkpoint_inspect": "options-forward-checkpoint-inspect",
            "history_inspect": "options-history-inspect",
            "chain_coverage_inspect": "options-chain-coverage-inspect",
            "quality_inspect": "options-research-quality-inspect",
        },
        "limitations": (
            "OPEN_INTEREST_UNAVAILABLE",
            "INDICATIVE_FEED_NOT_OPRA_CONSOLIDATED",
            "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",
        ),
        "warnings": OPTIONS_RESEARCH_OPERATIONS_WARNINGS,
        "authority": "NONE",
    }


def _universe_evidence_breakdown(security_reference_repository: Any) -> Any:
    """Bounded operator-visible rollup over every retained universe-snapshot
    row, current and legacy-compatible schemas alike, plus any row retained
    but genuinely incompatible with both.

    ``universe_snapshot_count`` is the exact retained-row count and must
    never be smaller than the sum of the three breakdown counts -- nothing
    here is silently excluded from the total the way a bare ``universes()``
    listing (which can only return rehydratable rows) would be.

    Returns a plain ``dict`` (like the rest of this repository-summary
    plumbing, which treats every repository as ``Any``): ``evidence`` (every
    ``RetainedUniverseSnapshotEvidence`` row), ``universes`` (the
    rehydratable subset, current and legacy-compatible), ``latest`` (the
    newest rehydratable snapshot or ``None``), ``latest_schema_compatibility``,
    and ``counts`` (the flat, JSON-ready breakdown merged into API/Console
    summaries).
    """
    from apex.adapters.security_reference_postgres import (
        SCHEMA_COMPATIBILITY_CURRENT,
        SCHEMA_COMPATIBILITY_INCOMPATIBLE,
        SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1,
    )

    evidence = security_reference_repository.universe_evidence()
    current = sum(
        1 for item in evidence if item.schema_compatibility == SCHEMA_COMPATIBILITY_CURRENT
    )
    legacy = sum(
        1
        for item in evidence
        if item.schema_compatibility == SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1
    )
    incompatible = tuple(
        {
            "idempotency_key": item.idempotency_key,
            "universe_id": item.universe_id,
            "payload_schema_marker": item.payload_schema_marker,
            "compatibility_blocker": item.compatibility_blocker,
        }
        for item in evidence
        if item.schema_compatibility == SCHEMA_COMPATIBILITY_INCOMPATIBLE
    )
    typed = tuple(item for item in evidence if item.snapshot is not None)
    latest_evidence = typed[-1] if typed else None
    return {
        "evidence": evidence,
        "universes": tuple(item.snapshot for item in typed),
        "latest": latest_evidence.snapshot if latest_evidence is not None else None,
        "latest_schema_compatibility": (
            latest_evidence.schema_compatibility if latest_evidence is not None else None
        ),
        "counts": {
            "universe_snapshot_count": len(evidence),
            "universe_snapshot_current_count": current,
            "universe_snapshot_legacy_compatible_count": legacy,
            "universe_snapshot_incompatible_count": len(incompatible),
            "incompatible_universe_snapshots": incompatible,
        },
    }


def _broad_market_reference_summary(
    security_reference_repository: Any,
    broad_equity_repository: Any,
    options_intelligence_repository: Any,
    research_evidence_repository: Any = None,
    security_identity_repository: Any = None,
    historical_market_data_repository: Any = None,
    genuine_multi_asset_repository: Any = None,
    historical_replay_repository: Any = None,
    corporate_fundamentals_repository: Any = None,
) -> dict[str, object]:
    """Bounded reference summary over the durable broad-market repositories.

    This is explanatory context only, in the same spirit as
    ``TOTAL_MARKET_CONTEXT_POLICY_V1``: it may never change Shared Opportunity
    eligibility, priority, allocation, or source explanation, and it adds no
    Campaign, Order, Position, reservation, journal, PAPER, or LIVE authority.
    Research-universe eligibility and provider execution capability are
    reported as separate fields throughout -- neither is inferred from the
    other, and an absent Alpaca credential never appears as a positive
    execution capability. Universe-snapshot counts include legacy-compatible
    and retained-incompatible rows so this summary can never silently shrink
    relative to what the repository actually retains.
    """
    del broad_equity_repository  # reserved: no durable bulk-discovery-list route yet
    from apex.adapters.alpaca_market_data import AlpacaMarketDataHttpAdapter
    from apex.adapters.options_reference import (
        MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
        NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
    )
    from apex.adapters.security_reference import (
        NASDAQ_LISTED_PATH,
        NASDAQ_OTHER_LISTED_PATH,
        NASDAQ_TRADER_HOST,
        SEC_DATA_HOST,
        SEC_TICKERS_PATH,
    )
    from apex.application.research_evidence import (
        NASDAQ_OPTIONS_PROVIDER,
        NASDAQ_PROVIDER,
        SEC_PROVIDER,
        latest_completed_capture,
    )

    breakdown = _universe_evidence_breakdown(security_reference_repository)
    latest = breakdown["latest"]
    # optionable_underlyings() retains one decision per canonical_security_id
    # ever recorded, across every universe build -- not just the latest one.
    # A security's canonical_security_id is not stable across rebuilds (it
    # depends on which source record is "preferred," which can change as
    # more sources are integrated), so an unfiltered sum silently blends
    # decisions from a superseded universe into what is otherwise a
    # latest-universe-scoped summary. Scope to exactly the latest universe's
    # own identities, matching total_considered/research_eligible_count.
    optionable = options_intelligence_repository.optionable_underlyings()
    if latest is not None:
        latest_security_ids = {item.canonical_security_id for item in latest.decisions}
        optionable = tuple(
            item for item in optionable if item.canonical_security_id in latest_security_ids
        )
    contract_count = options_intelligence_repository.contract_count()
    all_captures = (
        research_evidence_repository.captures() if research_evidence_repository is not None else ()
    )
    options_capture = latest_completed_capture(all_captures, NASDAQ_OPTIONS_PROVIDER)
    nasdaq_capture = latest_completed_capture(
        all_captures,
        NASDAQ_PROVIDER,
        source_locator=f"https://{NASDAQ_TRADER_HOST}{NASDAQ_LISTED_PATH}",
    )
    nasdaq_other_capture = latest_completed_capture(
        all_captures,
        NASDAQ_PROVIDER,
        source_locator=f"https://{NASDAQ_TRADER_HOST}{NASDAQ_OTHER_LISTED_PATH}",
    )
    sec_capture = latest_completed_capture(
        all_captures, SEC_PROVIDER, source_locator=f"https://{SEC_DATA_HOST}{SEC_TICKERS_PATH}"
    )
    listing_source_coverage = (
        "NASDAQ_AND_OTHER_LISTED_COMPLETE"
        if nasdaq_capture is not None and nasdaq_other_capture is not None
        else "NASDAQ_ONLY"
        if nasdaq_capture is not None
        else "OTHER_LISTED_ONLY"
        if nasdaq_other_capture is not None
        else "NO_LISTING_SOURCE_CAPTURED"
    )
    execution_counts = dict(latest.execution_capability_counts) if latest is not None else {}

    def _source_state(capture: Any) -> dict[str, object]:
        return {
            "capture_id": capture.capture_id if capture is not None else None,
            "status": capture.status.value if capture is not None else None,
            "retained_observations": (
                capture.retained_observations if capture is not None else None
            ),
            "observation_at": (
                capture.observation_at.isoformat()
                if capture is not None and capture.observation_at is not None
                else None
            ),
        }

    # Stable-identity/options-relink evidence -- entirely additive, bounded
    # (the linkage table is scoped to one distinct-raw-symbol-sized run, not
    # the underlying million-plus contract rows), and never required: an
    # older deployment or a caller that has not yet run
    # security-identity-projection-build simply sees every field below as
    # empty/None rather than failing.
    stable_identity_summary: dict[str, object] = {
        "stable_identity_count": 0,
        "identity_resolution_count": 0,
        "identity_resolution_state_counts": (),
        "latest_option_linkage_run_id": None,
        "option_linkage_distinct_raw_symbols": 0,
        "option_linkage_linked_count": 0,
        "option_linkage_unlinked_count": 0,
        "option_linkage_ambiguous_count": 0,
    }
    if security_identity_repository is not None:
        latest_run_id = security_identity_repository.latest_option_linkage_run_id()
        run_linkages = (
            security_identity_repository.option_linkages_for_run(latest_run_id)
            if latest_run_id is not None
            else ()
        )
        stable_identity_summary = {
            "stable_identity_count": security_identity_repository.stable_identity_count(),
            "identity_resolution_count": security_identity_repository.resolution_count(),
            "identity_resolution_state_counts": (
                security_identity_repository.resolution_state_counts()
            ),
            "latest_option_linkage_run_id": latest_run_id,
            "option_linkage_distinct_raw_symbols": len(run_linkages),
            "option_linkage_linked_count": sum(
                1 for item in run_linkages if item.outcome.value == "LINKED"
            ),
            "option_linkage_unlinked_count": sum(
                1 for item in run_linkages if item.outcome.value == "UNLINKED"
            ),
            "option_linkage_ambiguous_count": sum(
                1 for item in run_linkages if item.outcome.value == "AMBIGUOUS"
            ),
        }

    # Historical market-data evidence -- entirely additive and bounded (a few
    # summary counts plus the latest snapshot's identity), never required: a
    # deployment that has not yet run a capture plan simply sees every field
    # below as empty/None. The point-in-time-universe and survivorship states
    # are surfaced explicitly here, in the same spirit as
    # ``BROAD_MARKET_REFERENCE_SUMMARY_V1`` itself, so the "today's identities
    # applied retroactively" limitation is impossible to miss from this one
    # reference summary.
    historical_data_summary: dict[str, object] = {
        "historical_capture_plan_count": 0,
        "historical_dataset_snapshot_count": 0,
        "historical_quality_report_count": 0,
        "historical_latest_snapshot_id": None,
        "historical_latest_snapshot_completeness_state": None,
        "historical_latest_snapshot_point_in_time_universe_state": None,
        "historical_latest_snapshot_survivorship_state": None,
        "historical_latest_snapshot_observation_count": None,
    }
    if historical_market_data_repository is not None:
        from apex.application.historical_market_data import historical_market_data_warnings

        historical_plans = historical_market_data_repository.list_plans()
        historical_snapshots = historical_market_data_repository.list_snapshots()
        historical_reports = historical_market_data_repository.list_quality_reports()
        latest_snapshot = historical_snapshots[-1] if historical_snapshots else None
        historical_data_summary = {
            "historical_capture_plan_count": len(historical_plans),
            "historical_dataset_snapshot_count": len(historical_snapshots),
            "historical_quality_report_count": len(historical_reports),
            "historical_latest_snapshot_id": (
                latest_snapshot.snapshot_id if latest_snapshot is not None else None
            ),
            "historical_latest_snapshot_completeness_state": (
                latest_snapshot.completeness_state.value if latest_snapshot is not None else None
            ),
            "historical_latest_snapshot_point_in_time_universe_state": (
                latest_snapshot.point_in_time_universe_state.value
                if latest_snapshot is not None
                else None
            ),
            "historical_latest_snapshot_survivorship_state": (
                latest_snapshot.survivorship_state.value if latest_snapshot is not None else None
            ),
            "historical_latest_snapshot_observation_count": (
                latest_snapshot.observation_count if latest_snapshot is not None else None
            ),
            "historical_data_warnings": historical_market_data_warnings(),
        }

    # Genuine multi-asset cohort/cross-market summary (milestone section
    # 15) -- explanatory only, same authority boundary as everything above:
    # never changes eligibility, priority, or allocation, and always
    # authority NONE. Cohort/quality-gate/cross-market counts are resolved
    # only when the caller supplies the new repository; historical replay
    # run count is scoped to genuine multi-asset replay runs specifically
    # (those bound to the resolved equity snapshot), not every replay run
    # in the system.
    genuine_multi_asset_summary: dict[str, object] = {
        "genuine_multi_asset_cohort_policy_id": None,
        "genuine_multi_asset_cohort_member_count": None,
        "genuine_multi_asset_quality_gate_usable_count": None,
        "genuine_multi_asset_quality_gate_usable_with_warnings_count": None,
        "genuine_multi_asset_quality_gate_insufficient_count": None,
        "genuine_multi_asset_quality_gate_rejected_count": None,
        "genuine_multi_asset_cross_market_dataset_id": None,
        "genuine_multi_asset_common_overlap_start": None,
        "genuine_multi_asset_common_overlap_end": None,
        "genuine_multi_asset_tmc_component_symbols": (),
        "genuine_multi_asset_replay_run_count": None,
    }
    if genuine_multi_asset_repository is not None and historical_market_data_repository is not None:
        from apex.application.genuine_multi_asset_history import (
            resolve_genuine_multi_asset_registry,
        )
        from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

        registry = resolve_genuine_multi_asset_registry(
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            historical_repository=historical_market_data_repository,
            genuine_repository=genuine_multi_asset_repository,
            replay_repository=historical_replay_repository,
        )
        genuine_multi_asset_summary = {
            "genuine_multi_asset_cohort_policy_id": registry.cohort_policy_id,
            "genuine_multi_asset_cohort_member_count": registry.cohort_member_count,
            "genuine_multi_asset_quality_gate_usable_count": registry.quality_gate_usable_count,
            "genuine_multi_asset_quality_gate_usable_with_warnings_count": (
                registry.quality_gate_usable_with_warnings_count
            ),
            "genuine_multi_asset_quality_gate_insufficient_count": (
                registry.quality_gate_insufficient_count
            ),
            "genuine_multi_asset_quality_gate_rejected_count": registry.quality_gate_rejected_count,
            "genuine_multi_asset_cross_market_dataset_id": registry.cross_market_dataset_id,
            "genuine_multi_asset_common_overlap_start": registry.common_overlap_start,
            "genuine_multi_asset_common_overlap_end": registry.common_overlap_end,
            "genuine_multi_asset_tmc_component_symbols": registry.tmc_component_symbols,
            "genuine_multi_asset_replay_run_count": len(registry.replay_run_ids),
        }

    # Corporate Fundamental, Filing, and Event Intelligence v1 -- explanatory
    # only, same authority boundary as everything above: never changes
    # Shared Opportunity eligibility, priority, allocation, or source
    # explanation, and always authority NONE. Present (as zero/None) even
    # before any plan/capture has ever run, so Console rendering never
    # needs a special empty case.
    corporate_evidence_summary: dict[str, object] = {
        "corporate_evidence_plan_id": None,
        "corporate_evidence_member_count": None,
        "corporate_evidence_excluded_missing_cik_count": None,
        "corporate_evidence_latest_checkpoint_id": None,
        "corporate_evidence_latest_checkpoint_created_at": None,
        "corporate_evidence_processed_issuer_count": None,
        "corporate_evidence_remaining_issuer_count": None,
        "corporate_evidence_capture_complete": None,
        "corporate_evidence_latest_quality_report_id": None,
        "corporate_evidence_issuers_captured": None,
        "corporate_evidence_point_in_time_completeness": None,
        "corporate_evidence_latest_dataset_id": None,
        "corporate_evidence_concept_vocabulary_version": None,
    }
    if corporate_fundamentals_repository is not None:
        from apex.domain.corporate_fundamentals import (
            CONCEPT_VOCABULARY_VERSION,
            pending_members,
        )

        corporate_plan = corporate_fundamentals_repository.latest_plan()
        if corporate_plan is not None:
            checkpoints = corporate_fundamentals_repository.checkpoints_for_plan(
                corporate_plan.plan_id
            )
            latest_checkpoint = checkpoints[-1] if checkpoints else None
            processed_ciks = corporate_fundamentals_repository.processed_ciks(
                corporate_plan.plan_id
            )
            latest_quality_report = corporate_fundamentals_repository.latest_quality_report(
                corporate_plan.plan_id
            )
            latest_dataset = corporate_fundamentals_repository.latest_dataset_snapshot(
                corporate_plan.plan_id
            )
            corporate_evidence_summary = {
                "corporate_evidence_plan_id": corporate_plan.plan_id,
                "corporate_evidence_member_count": len(corporate_plan.members),
                "corporate_evidence_excluded_missing_cik_count": len(
                    corporate_plan.excluded_missing_cik
                ),
                "corporate_evidence_latest_checkpoint_id": (
                    latest_checkpoint.checkpoint_id if latest_checkpoint is not None else None
                ),
                "corporate_evidence_latest_checkpoint_created_at": (
                    latest_checkpoint.created_at if latest_checkpoint is not None else None
                ),
                "corporate_evidence_processed_issuer_count": len(processed_ciks),
                "corporate_evidence_remaining_issuer_count": len(
                    pending_members(corporate_plan, processed_ciks=processed_ciks)
                ),
                "corporate_evidence_capture_complete": (
                    latest_checkpoint.complete if latest_checkpoint is not None else False
                ),
                "corporate_evidence_latest_quality_report_id": (
                    latest_quality_report.report_id if latest_quality_report is not None else None
                ),
                "corporate_evidence_issuers_captured": (
                    latest_quality_report.issuers_captured
                    if latest_quality_report is not None
                    else None
                ),
                "corporate_evidence_point_in_time_completeness": (
                    latest_quality_report.point_in_time_completeness
                    if latest_quality_report is not None
                    else None
                ),
                "corporate_evidence_latest_dataset_id": (
                    latest_dataset.dataset_id if latest_dataset is not None else None
                ),
                "corporate_evidence_concept_vocabulary_version": CONCEPT_VOCABULARY_VERSION,
            }

    return {
        "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
        "universe_id": latest.universe_id if latest is not None else None,
        "as_of_date": latest.as_of_date if latest is not None else None,
        "total_considered": latest.total_considered if latest is not None else 0,
        "research_eligible_count": latest.research_eligible_count if latest is not None else 0,
        "execution_capability_counts": tuple(sorted(execution_counts.items())),
        "execution_capability_known_count": (
            latest.execution_capability_known_count if latest is not None else 0
        ),
        "execution_capability_unknown_count": (
            latest.execution_capability_unknown_count if latest is not None else 0
        ),
        "latest_universe_schema_compatibility": breakdown["latest_schema_compatibility"],
        **breakdown["counts"],
        "nasdaq_listed_source": _source_state(nasdaq_capture),
        "nasdaq_other_listed_source": _source_state(nasdaq_other_capture),
        "sec_company_tickers_source": _source_state(sec_capture),
        "listing_source_coverage": listing_source_coverage,
        "alpaca_credentials_available": AlpacaMarketDataHttpAdapter().credentials_available,
        "identities_requiring_review": len(
            security_reference_repository.identities_requiring_review()
        ),
        "underlyings_considered": len(optionable),
        "optionable_underlying_count": sum(item.optionable for item in optionable),
        "contract_count": contract_count,
        "options_source_schema_level": NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
        "options_capture_bound_bytes": MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
        "options_capture_actual_bytes": (
            options_capture.byte_count if options_capture is not None else None
        ),
        "options_capture_raw_sha256": (
            options_capture.raw_sha256 if options_capture is not None else None
        ),
        "options_capture_status": (
            options_capture.status.value if options_capture is not None else None
        ),
        **stable_identity_summary,
        **historical_data_summary,
        **genuine_multi_asset_summary,
        **corporate_evidence_summary,
        # Legacy optionable-underlying evidence (canonical_security_id-keyed,
        # unstable across source-preference changes) alongside this
        # milestone's stable-identity relink (canonical_security_id-stable),
        # side by side rather than one silently replacing the other.
        "legacy_vs_stable_optionable_underlying_comparison": {
            "legacy_optionable_underlying_count": sum(item.optionable for item in optionable),
            "stable_linked_underlying_count": stable_identity_summary[
                "option_linkage_linked_count"
            ],
        },
        "warning": (
            "REFERENCE SUMMARY ONLY — DOES NOT CHANGE OPPORTUNITY ELIGIBILITY, "
            "PRIORITY, ALLOCATION, OR SOURCE EXPLANATION"
        ),
        "authority": "NONE",
    }


def _retained_research_workspace(repository: Any) -> dict[str, object]:
    from apex.application.research_evidence import research_workspace

    return research_workspace(repository)


def _retained_crypto_research_workspace(repository: Any) -> dict[str, object]:
    from apex.application.crypto_campaign import campaign_workspace
    from apex.application.crypto_campaign_candidate import candidate_workspace
    from apex.application.crypto_campaign_candidate_2 import candidate_2_workspace
    from apex.application.crypto_campaign_generic import (
        generic_workspace,
        retained_candidate_view_models,
    )
    from apex.application.crypto_challenger import crypto_challenger_workspace
    from apex.application.crypto_dossier import crypto_dossier_workspace
    from apex.application.crypto_market_pulse import crypto_market_pulse_workspace
    from apex.application.crypto_research import crypto_research_workspace
    from apex.application.crypto_universe import crypto_universe_workspace

    if hasattr(repository, "generic_definitions"):
        workspace = candidate_2_workspace(repository)
        workspace["generic_operability"] = generic_workspace(
            definitions=repository.generic_definitions(),
            plans=repository.generic_plans(),
        )
        workspace["generic_candidate_views"] = retained_candidate_view_models(repository)
        if hasattr(repository, "product_captures"):
            workspace["multi_asset_discovery"] = crypto_universe_workspace(repository)
        if hasattr(repository, "refresh_plans"):
            workspace["market_pulse"] = crypto_market_pulse_workspace(repository)
        return workspace
    if hasattr(repository, "candidate_protocols"):
        return candidate_workspace(repository)
    if hasattr(repository, "campaigns"):
        return campaign_workspace(repository)
    if hasattr(repository, "challenger_protocols"):
        return crypto_challenger_workspace(repository)
    if hasattr(repository, "strategy_specifications"):
        return crypto_dossier_workspace(repository)
    return crypto_research_workspace(repository)


def _request_id(request: Request) -> str:
    return request.headers.get(
        "X-Request-ID", deterministic_id("operator_request", {"now": datetime.now(tz=UTC)})
    )


def _http_error(
    request: Request,
    status: int,
    code: str,
    message: str,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorResponse(
            error_code=code,
            message=message,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=_request_id(request),
        ).model_dump(),
    )


def _require_local_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        raise _http_error(
            request, 400, "ORIGIN_REQUIRED", "mutating requests require a local Origin"
        )
    if not origin.startswith("http://"):
        raise _http_error(request, 403, "ORIGIN_FORBIDDEN", "request origin is not local")
    authority = origin.removeprefix("http://")
    host, separator, port = authority.partition(":")
    if (
        not host
        or "/" in authority
        or "@" in authority
        or host not in LOCAL_HOSTS
        or (separator and (not port.isdecimal() or not 1 <= int(port) <= 65535))
    ):
        raise _http_error(request, 403, "ORIGIN_FORBIDDEN", "request origin is not local")


async def _mutation_guard(request: Request) -> None:
    _require_local_origin(request)
    key = request.headers.get("idempotency-key")
    if key is None or not key.strip() or len(key) > 160:
        raise _http_error(
            request, 400, "IDEMPOTENCY_KEY_REQUIRED", "mutating requests require an idempotency key"
        )


def _page(request: Request, limit: int, offset: int) -> None:
    if not 1 <= limit <= 100 or offset < 0:
        raise _http_error(
            request,
            400,
            "PAGINATION_INVALID",
            "pagination must use a limit from 1 to 100 and a non-negative offset",
        )


# ----------------------------------------------------------------------
# Operator Console Follow-Up Performance v1.
#
# ``operator_console_followups`` below used to call
# ``application.counterfactual_followup.PlanFollowUpObservations.execute()``
# against the real, unwrapped repositories -- identical in shape to what
# the recurring ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` cycle's own
# ``SyncFollowUpObservations`` uses, except that service is cursor-bounded
# (a handful of candidates per cycle) while this route always evaluated
# every retained decision's full candidate set. Measured against the real
# retained corpus (521 decisions, 1666 V2-policy candidate pairs): ~103
# wall-clock seconds, ~96 CPU-seconds, 1178 SQL statements for one GET.
#
# Root cause, measured with cProfile + call-count instrumentation, not
# merely inferred: ``PlanFollowUpObservations.execute()`` itself is pure,
# cheap computation (its own share of the 103s: ~0.02s). The entire cost is
# two of its three dependencies being called far more often than the data
# they return actually changes within one request:
#   1. ~99s: the CRYPTO baseline-availability check
#      (``_crypto_baseline_reference`` -> ``_CryptoFollowUpPriceSource.
#      daily_close()``/``latest_close_before()``) fires once per currently-
#      DUE CRYPTO candidate (measured: 648 calls) and each unmodified call
#      re-fetches *and re-deserializes* the entire retained
#      ``crypto_product_observation`` table (measured: 100 rows, ~31,000
#      embedded candles, ~0.1-0.15s) from scratch, with no caching.
#   2. ~1s: ``SharedOpportunitySubjectSource.projection()`` fires once per
#      retained decision (measured: 521 calls x ~2ms) instead of one
#      batched read.
#
# The three wrapper classes below satisfy PlanFollowUpObservations's own
# three dependency Protocols (``DecisionSource``,
# ``SharedOpportunitySubjectSource``, ``CryptoPriceSource`` --
# ``application.counterfactual_followup``) using data this route already
# fetched once, or a single batched/memoized read, instead of the real
# per-call-unbounded repositories. ``PlanFollowUpObservations`` itself, its
# selection logic in ``daily_close()``/``latest_close_before()``, and every
# file the recurring cycle's own ``SyncFollowUpObservations``/
# ``AssessFollowUpEvidenceQuality``/``COUNTERFACTUAL_FOLLOWUP_REFRESH`` task
# branch depends on receive zero changes -- these wrappers are only ever
# constructed here, for this one GET route, and are never passed to or
# shared with anything the recurring cycle uses. Every classification this
# produces (DUE/NOT_YET_DUE/UNSUPPORTED/PERMANENTLY_UNAVAILABLE, every
# reason code) is therefore exactly as live and exactly as correct as
# before -- same engine, same snapshot, same unmodified business logic --
# just no longer paying for the identical read hundreds of times over.
# ----------------------------------------------------------------------


class _PrefetchedDecisions:
    """Satisfies ``application.counterfactual_followup.DecisionSource``
    from a tuple this route already fetched once for its own
    ``decisions_by_id``, instead of letting ``PlanFollowUpObservations.
    execute()`` issue a second, identical ``all_decisions()`` read."""

    def __init__(self, decisions: tuple[CounterfactualDecision, ...]) -> None:
        self._decisions = decisions

    def all_decisions(self) -> tuple[CounterfactualDecision, ...]:
        return self._decisions


class _PrefetchedProjections:
    """Satisfies ``application.counterfactual_followup.
    SharedOpportunitySubjectSource`` from one batched prefetch
    (``PostgresSharedOpportunityRepository.projections_by_id``) instead of
    the one-``projection()``-call-per-decision pattern
    ``PlanFollowUpObservations.execute()`` otherwise drives."""

    def __init__(self, projections_by_id: dict[str, SharedOpportunityProjection]) -> None:
        self._by_id = projections_by_id

    def projection(self, projection_id: str) -> SharedOpportunityProjection | None:
        return self._by_id.get(projection_id)


class _MemoizedCryptoUniverseObservations:
    """Duck-types the one read method
    (``PostgresCryptoUniverseRepository.observations()``) that
    ``_CryptoFollowUpPriceSource`` depends on, fetching the real
    repository's result at most once per instance instead of once per
    ``daily_close()``/``latest_close_before()`` call -- see this module's
    own "Operator Console Follow-Up Performance v1" comment block above
    for the measured trace this fixes. ``PostgresCryptoUniverseRepository``
    itself is stateless (a thin query wrapper around the engine); this
    class is the only thing that is request-scoped."""

    def __init__(self, inner: PostgresCryptoUniverseRepository) -> None:
        self._inner = inner
        self._cached: tuple[CryptoProductObservation, ...] | None = None

    def observations(self) -> tuple[CryptoProductObservation, ...]:
        if self._cached is None:
            self._cached = self._inner.observations()
        return self._cached


# ----------------------------------------------------------------------
# Operator Console Home Performance v1.
#
# ``operator_console_home`` below (via ``overview_extensions_value``) used
# to construct ``PlanFollowUpObservations`` against the real, unwrapped
# repositories -- the identical defect ``operator_console_followups`` had
# before Operator Console Follow-Up Performance v1 above, at a separate,
# previously-untouched call site. Measured in isolation on unmodified
# ``main``: 87.33s. Fixed the same way: ``overview_extensions_value`` now
# uses the identical ``_PrefetchedDecisions``/``_PrefetchedProjections``/
# ``_MemoizedCryptoUniverseObservations`` wrappers above -- no new classes
# needed for that part.
#
# Profiling this route (cProfile + call-count instrumentation, not merely
# inferred) found a second, independent defect on the same request path:
# ``research_operations_value`` below -- called directly by both
# ``operator_console_home`` and ``operator_console_attention`` -- passes
# the real, unwrapped ``research_operations_repository``/
# ``research_operations_knowledge`` singletons into
# ``application.research_operations.research_operations_workspace``. That
# function's own ``duplicate_decision``/``_proposal_knowledge`` helpers
# (called once per retained proposal from its own comprehension, again
# once per retained proposal-priority row from its own nested
# ``research_review_queue`` call, and again from its ``agenda_items``
# scan) each re-fetch ``repository.proposals()`` and re-derive
# ``proposal_state()`` (-> ``repository.reviews()``) for every retained
# proposal from scratch, with zero caching. Measured directly against the
# real retained corpus (28 proposals, 54 agenda items, 27
# proposal-priority rows), unprofiled, isolating exactly this function:
# ~6.7-6.9s wall-clock across three repeated runs, with
# ``repository.reviews()`` alone called 1676 times (at most 28 of which
# can ever be distinct), ``repository.proposals()`` 213 times,
# ``knowledge_source.knowledge()`` (itself an ``inspect()`` catalog query
# plus four table scans) 55 times, ``repository.budget_events()`` 55
# times, and ``repository.recommendations()`` 29 times -- all against data
# that cannot change mid-request.
#
# ``_MemoizedResearchOperationsRepository``/``_MemoizedResearchKnowledgeSource``
# below satisfy the exact same ``application.research_operations.
# ResearchOperationsRepository``/``ResearchKnowledgeSource`` Protocols
# ``research_operations_workspace`` already depends on, memoizing only
# those five proven-hot reads and forwarding every other read and every
# write straight through unmodified. ``research_operations_workspace``,
# ``research_review_queue``, ``duplicate_decision``, ``_proposal_knowledge``,
# ``proposal_state``, ``experiment_queue_status``, and
# ``classify_research_proposal`` all receive zero line changes -- these
# wrappers are only ever constructed inside ``research_operations_value``,
# request-scoped, and are never shared with any mutation route (proposal
# generation, review, approval, queueing all keep using the real
# repository directly, unwrapped) or with the recurring operating cycle
# (which does not call ``research_operations_workspace`` at all).
# ----------------------------------------------------------------------


class _MemoizedResearchOperationsRepository:
    """Satisfies ``application.research_operations.
    ResearchOperationsRepository`` (used structurally, not by
    inheritance) by memoizing the five read methods measured -- by direct
    call-count instrumentation, not inferred -- to be invoked far more
    often than the data they return can actually change within one
    request: ``proposals()``/``recommendations()`` (each called with no
    arguments, so the whole result is cached after the first real call)
    and ``reviews()``/``budget_events()`` (each called with a
    ``proposal_id`` argument, so cached per distinct id). Every other read
    and every write forwards straight through to the real repository,
    unmodified -- see this module's own "Operator Console Home
    Performance v1" comment block above for the measured trace this
    fixes. Constructed fresh per request; the real
    ``PostgresResearchOperationsRepository`` it wraps is itself stateless
    (a thin query wrapper around the engine)."""

    def __init__(self, inner: PostgresResearchOperationsRepository) -> None:
        self._inner = inner
        self._proposals: tuple[ResearchProposal, ...] | None = None
        self._recommendations: tuple[ResearchRecommendation, ...] | None = None
        self._reviews_by_proposal_id: dict[str, tuple[ResearchProposalReview, ...]] = {}
        self._budget_events_by_proposal_id: dict[str, tuple[ResearchBudgetEvent, ...]] = {}

    # -- memoized reads --

    def proposals(self) -> tuple[ResearchProposal, ...]:
        if self._proposals is None:
            self._proposals = self._inner.proposals()
        return self._proposals

    def recommendations(self) -> tuple[ResearchRecommendation, ...]:
        if self._recommendations is None:
            self._recommendations = self._inner.recommendations()
        return self._recommendations

    def reviews(self, proposal_id: str) -> tuple[ResearchProposalReview, ...]:
        if proposal_id not in self._reviews_by_proposal_id:
            self._reviews_by_proposal_id[proposal_id] = self._inner.reviews(proposal_id)
        return self._reviews_by_proposal_id[proposal_id]

    def budget_events(self, proposal_id: str) -> tuple[ResearchBudgetEvent, ...]:
        if proposal_id not in self._budget_events_by_proposal_id:
            self._budget_events_by_proposal_id[proposal_id] = self._inner.budget_events(proposal_id)
        return self._budget_events_by_proposal_id[proposal_id]

    # -- passthrough reads: each measured at no more than a few dozen
    # calls (never redundant -- called once per distinct id) in this read
    # model, so memoizing them would not reduce a real call count. --

    def agenda_item(self, agenda_item_id: str) -> ResearchAgendaItem | None:
        return self._inner.agenda_item(agenda_item_id)

    def agenda_items(self) -> tuple[ResearchAgendaItem, ...]:
        return self._inner.agenda_items()

    def proposal(self, proposal_id: str) -> ResearchProposal | None:
        return self._inner.proposal(proposal_id)

    def recommendation_for_run(self, run_id: str) -> ResearchRecommendation | None:
        return self._inner.recommendation_for_run(run_id)

    def generator_run(self, generator_run_id: str) -> ResearchProposalGeneratorRun | None:
        return self._inner.generator_run(generator_run_id)

    def generator_runs(self) -> tuple[ResearchProposalGeneratorRun, ...]:
        return self._inner.generator_runs()

    def proposal_priority(self, proposal_id: str) -> ResearchProposalPriority | None:
        return self._inner.proposal_priority(proposal_id)

    def proposal_priorities(self) -> tuple[ResearchProposalPriority, ...]:
        return self._inner.proposal_priorities()

    def proposal_feasibility(self, proposal_id: str) -> ResearchProposalFeasibility | None:
        return self._inner.proposal_feasibility(proposal_id)

    def proposal_feasibilities(self) -> tuple[ResearchProposalFeasibility, ...]:
        return self._inner.proposal_feasibilities()

    def experiment_approval(self, proposal_id: str) -> ResearchExperimentApproval | None:
        return self._inner.experiment_approval(proposal_id)

    def queue_item(self, queue_id: str) -> ResearchExperimentQueueItem | None:
        return self._inner.queue_item(queue_id)

    def queue_items(self) -> tuple[ResearchExperimentQueueItem, ...]:
        return self._inner.queue_items()

    def queue_events(self, queue_id: str) -> tuple[ResearchExperimentQueueEvent, ...]:
        return self._inner.queue_events(queue_id)

    def first_method_selection(self) -> FirstGeneratedResearchMethodSelection | None:
        return self._inner.first_method_selection()

    def first_method_implementation(self) -> FirstGeneratedResearchMethodImplementation | None:
        return self._inner.first_method_implementation()

    def first_method_run(self, method_run_id: str) -> FirstGeneratedResearchMethodRun | None:
        return self._inner.first_method_run(method_run_id)

    def first_method_runs(self) -> tuple[FirstGeneratedResearchMethodRun, ...]:
        return self._inner.first_method_runs()

    # -- write passthroughs: this wrapper is only ever constructed for the
    # read-only workspace projection below and never handed to a mutation
    # route, but every ``ResearchOperationsRepository`` Protocol member
    # must exist for structural typing to accept passing this in its
    # place. --

    def save_agenda_item(self, value: ResearchAgendaItem) -> bool:
        return self._inner.save_agenda_item(value)

    def save_proposal(self, value: ResearchProposal) -> bool:
        return self._inner.save_proposal(value)

    def save_review(self, value: ResearchProposalReview) -> bool:
        return self._inner.save_review(value)

    def save_budget_event(self, value: ResearchBudgetEvent) -> bool:
        return self._inner.save_budget_event(value)

    def save_recommendation(self, value: ResearchRecommendation) -> bool:
        return self._inner.save_recommendation(value)

    def save_generator_run(self, value: ResearchProposalGeneratorRun) -> bool:
        return self._inner.save_generator_run(value)

    def save_proposal_priority(self, value: ResearchProposalPriority) -> bool:
        return self._inner.save_proposal_priority(value)

    def save_proposal_feasibility(self, value: ResearchProposalFeasibility) -> bool:
        return self._inner.save_proposal_feasibility(value)

    def save_experiment_approval(self, value: ResearchExperimentApproval) -> bool:
        return self._inner.save_experiment_approval(value)

    def save_queue_item(self, value: ResearchExperimentQueueItem) -> bool:
        return self._inner.save_queue_item(value)

    def save_queue_event(self, value: ResearchExperimentQueueEvent) -> bool:
        return self._inner.save_queue_event(value)

    def save_first_method_selection(self, value: FirstGeneratedResearchMethodSelection) -> bool:
        return self._inner.save_first_method_selection(value)

    def save_first_method_implementation(
        self, value: FirstGeneratedResearchMethodImplementation
    ) -> bool:
        return self._inner.save_first_method_implementation(value)

    def save_first_method_run(self, value: FirstGeneratedResearchMethodRun) -> bool:
        return self._inner.save_first_method_run(value)


class _MemoizedResearchKnowledgeSource:
    """Satisfies ``application.research_operations.ResearchKnowledgeSource``
    by fetching ``PostgresRetainedResearchKnowledgeSource.knowledge()`` (an
    ``inspect()`` catalog query plus four table scans) at most once per
    instance instead of once per ``duplicate_decision()`` call -- see this
    module's own "Operator Console Home Performance v1" comment block
    above for the measured trace this fixes."""

    def __init__(self, inner: PostgresRetainedResearchKnowledgeSource) -> None:
        self._inner = inner
        self._cached: tuple[PriorResearchKnowledge, ...] | None = None

    def knowledge(self) -> tuple[PriorResearchKnowledge, ...]:
        if self._cached is None:
            self._cached = self._inner.knowledge()
        return self._cached


# ----------------------------------------------------------------------
# Operator Console Live Readiness Performance v1.
#
# ``operator_console_live_readiness`` and ``operator_console_live_preflight``
# below both call ``application.live_preflight.live_preflight_workspace()``
# against the real, unwrapped ``live_preflight_repository``. That function's
# own ``active_intents`` computation calls ``repository.invalidations(
# item.intent_id)`` once per retained ``LiveIntentProposal`` to determine
# which intents remain active for the ``LIVE_INTENT_ELIGIBILITY_REVIEW``
# operating-cycle task, and ``PostgresLivePreflightRepository.invalidations()``
# is a fully unfiltered ``SELECT * FROM live_intent_invalidation`` (no SQL
# ``WHERE`` clause at all) that re-fetches and re-deserializes every retained
# invalidation on every call, then filters in Python -- an
# intents() x invalidations() pattern, worse in that one respect than the
# Operator Console Follow-Up/Home Performance v1 defects above (those at
# least filtered in SQL).
#
# Genuine retained state today is zero intents and zero invalidations (no
# Shared Opportunity has ever reached genuine LIVE eligibility), so this
# does not yet cost anything measurable in production -- but it is unbounded
# in both factors, not merely slow at today's scale. Measured directly
# (cProfile + call-count instrumentation, seeded via the real
# ``ConstructLiveIntent``/``InvalidateLiveIntent`` application services
# against a real ``PostgresLivePreflightRepository``, never the retained
# ``apex`` database): 0.02s at today's real 0/0 shape, 1.43s at 200
# intents/20 invalidations, 6.90s at 500 intents/50 invalidations (already
# past this milestone's 5s hard ceiling) -- confirming the O(intents x
# invalidations) shape, not merely inferring it from source.
#
# ``_MemoizedLivePreflightInvalidations`` below satisfies the exact
# ``application.live_preflight.LivePreflightRepository`` Protocol
# ``live_preflight_workspace`` already depends on, fetching the retained
# invalidation table at most once per instance (via the new, additive
# ``PostgresLivePreflightRepository.invalidations_by_intent_id()`` -- the
# same underlying query ``invalidations()`` itself issues, just once instead
# of once per intent) and indexing it in memory; every other read and every
# write forwards straight through to the real repository, unmodified.
# ``live_preflight_workspace``, ``build_live_preflight_operating_tasks``, and
# every mutation service in ``application.live_preflight`` receive zero line
# changes -- this wrapper is only ever constructed inside the two GET routes
# below, request-scoped, and is never shared with any mutation route or with
# the recurring operating cycle (which does not call
# ``live_preflight_workspace`` at all).
# ----------------------------------------------------------------------


class _MemoizedLivePreflightInvalidations:
    """Satisfies ``application.live_preflight.LivePreflightRepository``
    (used structurally, not by inheritance) by memoizing ``invalidations()``
    -- measured to be called once per retained intent by
    ``live_preflight_workspace``, with the real implementation re-scanning
    the entire ``live_intent_invalidation`` table unfiltered on every call --
    via one batched, additive read
    (``PostgresLivePreflightRepository.invalidations_by_intent_id()``).
    Every other read and every write forwards straight through to the real
    repository, unmodified -- see this module's own "Operator Console Live
    Readiness Performance v1" comment block above for the measured trace
    this fixes. Constructed fresh per request; the real
    ``PostgresLivePreflightRepository`` it wraps is itself stateless (a thin
    query wrapper around the engine)."""

    def __init__(self, inner: PostgresLivePreflightRepository) -> None:
        self._inner = inner
        self._by_intent_id: dict[str, tuple[LiveIntentInvalidation, ...]] | None = None

    def invalidations(self, intent_id: str) -> tuple[LiveIntentInvalidation, ...]:
        if self._by_intent_id is None:
            self._by_intent_id = self._inner.invalidations_by_intent_id()
        return self._by_intent_id.get(intent_id, ())

    # -- passthrough reads/writes: this wrapper is only ever constructed for
    # the read-only workspace projections below and never handed to a
    # mutation route, but every ``LivePreflightRepository`` Protocol member
    # must exist for structural typing to accept passing this in its place.
    # None of these are called more than once per distinct id in this read
    # model, so memoizing them would not reduce a real call count. --

    def save_intent(self, value: LiveIntentProposal) -> LiveIntentProposal:
        return self._inner.save_intent(value)

    def intent(self, intent_id: str) -> LiveIntentProposal | None:
        return self._inner.intent(intent_id)

    def intents(self) -> tuple[LiveIntentProposal, ...]:
        return self._inner.intents()

    def save_invalidation(self, value: LiveIntentInvalidation) -> LiveIntentInvalidation:
        return self._inner.save_invalidation(value)

    def save_eligibility(
        self, value: LiveIntentEligibilityDecision
    ) -> LiveIntentEligibilityDecision:
        return self._inner.save_eligibility(value)

    def eligibility(self, decision_id: str) -> LiveIntentEligibilityDecision | None:
        return self._inner.eligibility(decision_id)

    def save_preflight(self, value: InternalPreflight) -> InternalPreflight:
        return self._inner.save_preflight(value)

    def preflight(self, preflight_id: str) -> InternalPreflight | None:
        return self._inner.preflight(preflight_id)

    def preflights(self) -> tuple[InternalPreflight, ...]:
        return self._inner.preflights()

    def save_preview_approval(self, value: ProviderPreviewApproval) -> ProviderPreviewApproval:
        return self._inner.save_preview_approval(value)

    def preview_approval(self, approval_id: str) -> ProviderPreviewApproval | None:
        return self._inner.preview_approval(approval_id)

    def preview_approvals(self) -> tuple[ProviderPreviewApproval, ...]:
        return self._inner.preview_approvals()

    def save_preview_capture(self, value: ProviderPreviewCapture) -> ProviderPreviewCapture:
        return self._inner.save_preview_capture(value)

    def preview_capture(self, capture_id: str) -> ProviderPreviewCapture | None:
        return self._inner.preview_capture(capture_id)

    def preview_captures(self) -> tuple[ProviderPreviewCapture, ...]:
        return self._inner.preview_captures()

    def save_preview_attempt(self, value: ProviderPreviewAttempt) -> ProviderPreviewAttempt:
        return self._inner.save_preview_attempt(value)

    def preview_attempt(self, attempt_id: str) -> ProviderPreviewAttempt | None:
        return self._inner.preview_attempt(attempt_id)

    def preview_attempts(self) -> tuple[ProviderPreviewAttempt, ...]:
        return self._inner.preview_attempts()

    def save_assessment(self, value: LivePreflightAssessment) -> LivePreflightAssessment:
        return self._inner.save_assessment(value)

    def assessment(self, assessment_id: str) -> LivePreflightAssessment | None:
        return self._inner.assessment(assessment_id)

    def assessments(self) -> tuple[LivePreflightAssessment, ...]:
        return self._inner.assessments()


def create_app() -> FastAPI:
    """Create a same-origin localhost app; Uvicorn bind policy lives in dev.py."""

    engine = create_postgres_engine(PostgresSettings.from_env())
    store = postgres_operator_console_store(engine)
    shadow_operations_store = postgres_shadow_operations_store(engine)
    evidence_review_store = postgres_evidence_review_store(engine)
    strategy_lab_store = PostgresStrategyLabStore(engine)
    strategy_lab_shadow_store = PostgresStrategyLabShadowStore(engine)
    strategy_lab_artifacts = LocalStrategyLabArtifactStore()
    strategy_lab_service = StrategyLabService(strategy_lab_store, strategy_lab_artifacts)
    strategy_lab_shadow_service = StrategyLabShadowService(
        strategy_lab_store,
        strategy_lab_shadow_store,
        strategy_lab_artifacts,
        StrategyLabManagedModelLoader(),
    )
    securities_repository = PostgresSecuritiesRepository(engine)
    securities_direction_dataset_repository = PostgresSecuritiesDirectionDatasetRepository(engine)
    strategy_intelligence_repository = PostgresStrategyIntelligenceRepository(engine)
    ensemble_repository = PostgresEnsembleRepository(engine)
    real_securities_repository = PostgresRealSecuritiesDataRepository(engine)
    securities_baseline_repository = PostgresSecuritiesBaselineRepository(engine)
    research_integrity_repository = PostgresResearchIntegrityRepository(engine)
    research_evidence_repository = PostgresResearchEvidenceRepository(engine)
    security_reference_repository = PostgresSecurityReferenceRepository(engine)
    broad_equity_repository = PostgresBroadEquityRepository(engine)
    options_intelligence_repository = PostgresOptionsIntelligenceRepository(engine)
    security_identity_repository = PostgresSecurityIdentityRepository(engine)
    historical_market_data_repository = PostgresHistoricalMarketDataRepository(engine)
    historical_replay_repository = PostgresHistoricalReplayRepository(engine)
    research_methodology_repository = PostgresResearchMethodologyRepository(engine)
    genuine_multi_asset_repository = PostgresGenuineMultiAssetHistoryRepository(engine)
    cross_market_research_repository = PostgresCrossMarketResearchRepository(engine)
    research_calibration_repository = PostgresCalibrationRepository(engine)
    research_calibration_v2_repository = PostgresCalibrationV2Repository(engine)
    research_replication_repository = PostgresResearchReplicationRepository(engine)
    options_quote_intelligence_repository = PostgresOptionsQuoteIntelligenceRepository(engine)
    options_structures_repository = PostgresOptionsStructuresRepository(engine)
    options_research_operations_repository = PostgresOptionsResearchOperationsRepository(engine)
    counterfactual_ledger_repository = PostgresCounterfactualLedgerRepository(engine)
    counterfactual_followup_repository = PostgresCounterfactualFollowUpRepository(engine)
    # Operator Console Follow-Up Performance v1: a second, independent,
    # stateless handle onto the same retained crypto-observation evidence
    # ``counterfactual_followup_repository.crypto_prices`` already reads --
    # used only to build a fresh, request-scoped memoized wrapper per
    # ``/followups`` call (see ``_MemoizedCryptoUniverseObservations``
    # above). Never shared with, and does not change, anything the
    # recurring cycle depends on.
    crypto_universe_repository = PostgresCryptoUniverseRepository(engine)
    portfolio_state_repository = PostgresPortfolioSnapshotRepository(engine)
    investment_mandate_repository = PostgresInvestmentMandateRepository(engine)
    corporate_fundamentals_repository = PostgresCorporateFundamentalsRepository(engine)
    corporate_calibration_depth_repository = PostgresCorporateCalibrationDepthRepository(engine)
    corporate_regime_followup_repository = PostgresCorporateRegimeFollowUpRepository(engine)
    crypto_research_evidence_repository = PostgresCryptoResearchRepository(engine)
    crypto_research_repository = PostgresCryptoMarketPulseRepository(engine)
    crypto_stateful_research_repository = PostgresCryptoStatefulResearchRepository(engine)
    crypto_paper_repository = PostgresCryptoPaperRepository(engine)
    cross_venue_allocation_repository = PostgresCrossVenueAllocationRepository(engine)
    kalshi_operating_repository = PostgresKalshiOperatingRepository(engine)
    shared_opportunity_repository = PostgresSharedOpportunityRepository(engine)
    total_market_context_repository = PostgresTotalMarketContextRepository(engine)
    operating_cycle_repository = PostgresOperatingCycleRepository(engine)
    performance_attribution_repository = PostgresPerformanceAttributionRepository(engine)
    live_readiness_repository = PostgresLiveReadinessRepository(engine)
    live_preflight_repository = PostgresLivePreflightRepository(engine)
    operational_reliability_repository = PostgresOperationalReliabilityRepository(engine)
    coinbase_read_only_repository = PostgresCoinbaseReadOnlyRepository(engine)
    kalshi_read_only_repository = PostgresKalshiReadOnlyRepository(engine)
    research_operations_repository = PostgresResearchOperationsRepository(engine)
    research_operations_knowledge = PostgresRetainedResearchKnowledgeSource(engine)
    # Research Experiment Executability v1: read-only sources for the one
    # already-QUALIFIED aggregate a ``COUNTERFACTUAL_OUTCOME_POLICY_REVIEW``/
    # ``SECURITIES_STRUCTURE_OUTCOME_POLICY_REVIEW`` proposal is anchored to
    # -- shared (not re-instantiated per request) since both are stateless
    # read repositories, matching every other repository on this line.
    research_outcome_aggregate_executor = RetainedOutcomeAggregateExperimentExecutor(
        counterfactual_outcome=PostgresCounterfactualOutcomeRepository(engine),
        securities_structure_outcome=PostgresSecuritiesStructureOutcomeRepository(engine),
    )
    research_integrity_artifacts = LocalResearchReturnArtifactStore()
    research_integrity_service = ResearchIntegrityService(
        research_integrity_repository,
        research_integrity_artifacts,
    )
    candidate_generation_repository = PostgresCandidateGenerationRepository(engine)
    batch_learning_repository = PostgresBatchLearningRepository(engine)
    securities_data = LocalSecuritiesDatasetAdapter()
    securities_campaigns = PostgresSecuritiesCampaignBoundary(engine)
    lean_securities_engine = LeanSecuritiesEngineAdapter()
    securities_paper_repository = PostgresSecuritiesPaperRepository(engine)
    external_paper_repository = PostgresExternalPaperRepository(engine)
    live_observation_repository = PostgresLiveMarketObservationRepository(engine)
    kalshi_resolution_repository = PostgresKalshiResolutionFactRepository(engine)
    kalshi_baseline_quote_repository = PostgresKalshiBaselineQuoteRepository(engine)
    # The Operator API is acceptance-fixture-only.  Keeping this adapter in the
    # process makes every route deterministic and prevents a console request
    # from reaching credentials, a provider, or an Alpaca PAPER endpoint.
    fixture_paper_broker = DeterministicPaperBrokerFixture(scenarios={}, now=datetime.now(tz=UTC))
    sustained_shadow_repository = PostgresSustainedShadowRepository(engine)
    sustained_shadow_operations_repository = PostgresSustainedShadowOperationsRepository(engine)
    portfolio_intelligence_repository = PostgresPortfolioIntelligenceRepository(engine)
    securities_paper_campaigns = PostgresSecuritiesPaperCampaignBoundary(engine)
    lean_securities_paper = LeanSecuritiesPaperAdapter()

    def ensure_evidence_review_policies() -> None:
        """Seed immutable initial policy definitions only when the review API is used.

        Creating the local ASGI application remains an offline operation so the
        core test boundary never needs PostgreSQL merely to import its routes.
        """

        for policy in initial_evidence_review_policies(datetime(2026, 1, 1, tzinfo=UTC)):
            evidence_review_store.ensure_policy(policy)

    def ensure_securities_baselines() -> None:
        for specification in initial_securities_strategy_specifications(
            datetime(2026, 1, 1, tzinfo=UTC)
        ):
            securities_repository.save_specification(specification)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.store = store
    app.state.evidence_review_store = evidence_review_store
    app.state.strategy_lab_store = strategy_lab_store
    app.state.strategy_lab_shadow_store = strategy_lab_shadow_store
    app.state.securities_repository = securities_repository
    app.state.securities_direction_dataset_repository = securities_direction_dataset_repository
    app.state.strategy_intelligence_repository = strategy_intelligence_repository
    app.state.ensemble_repository = ensemble_repository
    app.state.real_securities_repository = real_securities_repository
    app.state.securities_baseline_repository = securities_baseline_repository
    app.state.external_paper_repository = external_paper_repository
    app.state.research_integrity_repository = research_integrity_repository
    app.state.research_evidence_repository = research_evidence_repository
    app.state.security_reference_repository = security_reference_repository
    app.state.broad_equity_repository = broad_equity_repository
    app.state.options_intelligence_repository = options_intelligence_repository
    app.state.security_identity_repository = security_identity_repository
    app.state.historical_market_data_repository = historical_market_data_repository
    app.state.historical_replay_repository = historical_replay_repository
    app.state.research_methodology_repository = research_methodology_repository
    app.state.cross_market_research_repository = cross_market_research_repository
    app.state.research_calibration_repository = research_calibration_repository
    app.state.research_calibration_v2_repository = research_calibration_v2_repository
    app.state.research_replication_repository = research_replication_repository
    app.state.options_quote_intelligence_repository = options_quote_intelligence_repository
    app.state.options_structures_repository = options_structures_repository
    app.state.options_research_operations_repository = options_research_operations_repository
    app.state.counterfactual_ledger_repository = counterfactual_ledger_repository
    app.state.counterfactual_followup_repository = counterfactual_followup_repository
    app.state.portfolio_state_repository = portfolio_state_repository
    app.state.investment_mandate_repository = investment_mandate_repository
    app.state.crypto_paper_repository = crypto_paper_repository
    app.state.kalshi_operating_repository = kalshi_operating_repository
    app.state.candidate_generation_repository = candidate_generation_repository
    app.state.batch_learning_repository = batch_learning_repository
    app.state.securities_paper_repository = securities_paper_repository
    app.state.sustained_shadow_repository = sustained_shadow_repository
    app.state.sustained_shadow_operations_repository = sustained_shadow_operations_repository
    app.state.portfolio_intelligence_repository = portfolio_intelligence_repository

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, error: HTTPException) -> JSONResponse:
        detail = error.detail
        if isinstance(detail, dict) and "error_code" in detail:
            return JSONResponse(status_code=error.status_code, content=detail)
        return JSONResponse(
            status_code=error.status_code,
            content=ErrorResponse(
                error_code="REQUEST_REJECTED",
                message="request was rejected",
                request_id=_request_id(request),
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error_code="MALFORMED_REQUEST",
                message="request does not match the local operator contract",
                request_id=_request_id(request),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code="INTERNAL_ERROR",
                message="the local operator service could not complete the request",
                request_id=_request_id(request),
            ).model_dump(),
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            ready = True
        except Exception:
            ready = False
        return _envelope(
            {
                "service_state": "READY" if ready else "DATABASE_UNAVAILABLE",
                "database_ready": ready,
                "runtime_capability": "SIMULATION_ONLY",
                "simulation_only": True,
            }
        )

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict[str, object]:
        return _envelope(
            {
                "shadow_enabled": True,
                "paper_enabled": True,
                "live_enabled": False,
                "provider_submission_enabled": False,
                "credentials_enabled": False,
            }
        )

    @app.get("/api/v1/research-integrity/experiment-families")
    def research_experiment_families() -> dict[str, object]:
        families = research_integrity_repository.families()
        return _envelope(
            {
                "warnings": (
                    "TESTING MORE VARIANTS INCREASES THE CHANCE OF A FALSE WINNER",
                    "RESEARCH INTEGRITY ELIGIBILITY DOES NOT GRANT TRADING AUTHORITY",
                ),
                "items": tuple(
                    {
                        "family": family,
                        "trial_budget_used": len(
                            research_integrity_repository.trials(family.experiment_id)
                        ),
                    }
                    for family in families
                ),
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.get("/api/v1/strategy-intelligence/runs")
    def strategy_intelligence_runs() -> dict[str, object]:
        """Read immutable historical research results; no execution controls exist."""

        runs = strategy_intelligence_repository.runs()
        latest = runs[-1] if runs else None
        snapshot: object | None = None
        evaluation: object | None = None
        if latest is not None:
            try:
                snapshot = json.loads(
                    strategy_lab_artifacts.read_bytes(latest.latest_snapshot_artifact)
                )
                evaluation = json.loads(
                    strategy_lab_artifacts.read_bytes(latest.evaluation_artifact)
                )
            except (ValueError, OSError):
                snapshot = {"state": "ARTIFACT_UNAVAILABLE"}
                evaluation = {"state": "ARTIFACT_UNAVAILABLE"}
        return _envelope(
            {
                "warnings": (
                    "STRATEGY INTELLIGENCE IS RESEARCH ONLY",
                    "ABSTENTION IS A VALID DECISION",
                    "GENUINE HISTORICAL PERFORMANCE DOES NOT ESTABLISH A TRADING EDGE",
                ),
                "items": runs,
                "latest_snapshot": snapshot,
                "latest_evaluation": evaluation,
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.get("/api/v1/ensemble-meta-strategy/runs")
    def ensemble_meta_strategy_runs() -> dict[str, object]:
        runs = ensemble_repository.runs()
        latest = runs[-1] if runs else None
        snapshot: object | None = None
        evaluation: object | None = None
        if latest is not None:
            try:
                snapshot = json.loads(strategy_lab_artifacts.read_bytes(latest.snapshot_artifact))
                evaluation = json.loads(
                    strategy_lab_artifacts.read_bytes(latest.evaluation_artifact)
                )
            except Exception:
                snapshot = {"state": "ARTIFACT_UNAVAILABLE"}
                evaluation = {"state": "ARTIFACT_UNAVAILABLE"}
        return _envelope(
            {
                "warnings": (
                    "META-STRATEGY OUTPUT IS RESEARCH ONLY",
                    "BLOCKED MODELS CANNOT GRANT ACTION AUTHORITY",
                    "CASH AND ABSTENTION ARE VALID OUTCOMES",
                ),
                "items": runs,
                "latest_snapshot": snapshot,
                "latest_evaluation": evaluation,
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.post(
        "/api/v1/research-integrity/experiment-families",
        dependencies=[Depends(_mutation_guard)],
    )
    def create_research_experiment_family(
        body: ResearchExperimentFamilyRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        canonical_families = {
            item.family.value
            for item in canonical_securities_baselines(datetime(2026, 1, 1, tzinfo=UTC))
        }
        if not set(body.permitted_families).issubset(canonical_families):
            raise _http_error(
                request,
                422,
                "RESEARCH_FAMILY_SCOPE_REJECTED",
                "permitted families must use managed canonical Securities baselines",
            )
        try:
            batch = securities_repository.get_batch(body.data_batch_id)
            family = OpenResearchExperimentFamily(research_integrity_repository).execute(
                name=body.name,
                vertical=body.vertical,
                research_family="CANONICAL_SECURITIES_BASELINES",
                research_question=body.research_question,
                target_label_identity=body.target_label_identity,
                benchmark_identities=body.benchmark_identities,
                data_scope_identities=(body.data_batch_id,),
                data_scope_hashes=(batch.canonical_hash,),
                universe_hash=content_digest({"managed_universe_id": body.universe_id}),
                period_start=body.start,
                period_end=body.end,
                cutoff_at=datetime.now(tz=UTC),
                feature_schema_hashes=(),
                permitted_families=body.permitted_families,
                parameter_space={
                    "managed_specification": body.managed_specification,
                    "baseline_families": tuple(body.permitted_families),
                    "simulator": ("APEX_RESEARCH", "VECTORBT", "LEAN"),
                },
                maximum_trials=body.maximum_trials,
                maximum_feature_variants=body.maximum_feature_variants,
                maximum_model_variants=body.maximum_model_variants,
                maximum_simulator_variants=body.maximum_simulator_variants,
                evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
                evaluation_policy_payload={
                    "minimum_psr": "0.95",
                    "minimum_dsr": "0.80",
                    "maximum_pbo": "0.25",
                },
                cost_execution_assumptions={
                    "source": "canonical-securities-baseline-request",
                    "alignment": "exact",
                },
                frequency=ResearchFrequency.DAILY,
                operator_action_id=f"api:{idempotency_key}",
                operator_reason=body.operator_reason,
                idempotency_key=idempotency_key,
            )
            return _envelope(
                family,
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except (ResearchIntegrityApplicationError, ValueError) as error:
            raise _http_error(
                request,
                422,
                "RESEARCH_FAMILY_REJECTED",
                str(error),
            ) from error

    @app.post(
        "/api/v1/research-integrity/experiment-families/{experiment_id}/close",
        dependencies=[Depends(_mutation_guard)],
    )
    def close_research_experiment_family(
        experiment_id: str,
        body: ResearchExperimentTransitionRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            family = ResearchExperimentLifecycle(research_integrity_repository).transition(
                experiment_id,
                target=ResearchFamilyState.CLOSED,
                reason=body.operator_reason,
                changed_at=datetime.now(tz=UTC),
            )
            return _envelope(family, capability="RESEARCH_EVIDENCE_ONLY")
        except ResearchIntegrityApplicationError as error:
            raise _http_error(request, 422, "RESEARCH_FAMILY_CLOSE_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/research-integrity/experiment-families/{experiment_id}/cancel",
        dependencies=[Depends(_mutation_guard)],
    )
    def cancel_research_experiment_family(
        experiment_id: str,
        body: ResearchExperimentTransitionRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            family = ResearchExperimentLifecycle(research_integrity_repository).transition(
                experiment_id,
                target=ResearchFamilyState.CANCELED,
                reason=body.operator_reason,
                changed_at=datetime.now(tz=UTC),
            )
            return _envelope(family, capability="RESEARCH_EVIDENCE_ONLY")
        except ResearchIntegrityApplicationError as error:
            raise _http_error(
                request, 422, "RESEARCH_FAMILY_CANCEL_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/research-integrity/experiment-families/{experiment_id}/trials")
    def research_experiment_trials(experiment_id: str, request: Request) -> dict[str, object]:
        if research_integrity_repository.family(experiment_id) is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_FAMILY_NOT_FOUND",
                "experiment family was not found",
            )
        trials = research_integrity_repository.trials(experiment_id)
        return _envelope(
            {
                "failed_trials_hidden": False,
                "trial_deletion_supported": False,
                "items": tuple(
                    {
                        "trial": trial,
                        "metric": research_integrity_repository.metric(trial.trial_id),
                        "artifacts": research_integrity_repository.artifacts(trial.trial_id),
                    }
                    for trial in trials
                ),
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.post(
        "/api/v1/research-integrity/experiment-families/{experiment_id}/evaluations",
        dependencies=[Depends(_mutation_guard)],
    )
    def evaluate_research_experiment_family(
        experiment_id: str,
        body: ResearchOverfittingEvaluationRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            evaluation = research_integrity_service.evaluate(
                experiment_id=experiment_id,
                cutoff_at=body.cutoff_at,
            )
            return _envelope(
                evaluation,
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except (ResearchIntegrityApplicationError, ValueError) as error:
            raise _http_error(
                request,
                422,
                "RESEARCH_EVALUATION_REJECTED",
                str(error),
            ) from error

    @app.get("/api/v1/research-integrity/experiment-families/{experiment_id}/evaluations")
    def research_experiment_evaluations(experiment_id: str, request: Request) -> dict[str, object]:
        if research_integrity_repository.family(experiment_id) is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_FAMILY_NOT_FOUND",
                "experiment family was not found",
            )
        return _envelope(
            {
                "warnings": (
                    "BEST OBSERVED RESULT MAY REFLECT RESEARCH SELECTION",
                    "RESEARCH ELIGIBILITY IS NOT STRATEGY OR CAPITAL AUTHORITY",
                ),
                "items": research_integrity_repository.evaluations(experiment_id),
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    def _batch_cycle_detail(cycle: BatchLearningCycle) -> dict[str, object]:
        cycle_id = cycle.cycle_id
        forward_window_id = cycle.forward_window_id
        window = (
            batch_learning_repository.forward_window(str(forward_window_id))
            if forward_window_id is not None
            else None
        )
        return {
            "cycle": cycle,
            "binding": batch_learning_repository.binding(cycle_id),
            "forward_window": window,
            "decisions": batch_learning_repository.decisions(cycle_id),
            "challenger_evaluation": (
                strategy_lab_shadow_store.challenger_evaluation(window.challenger_deployment_id)
                if window is not None
                else None
            ),
        }

    @app.get("/api/v1/strategy-lab/learning-plans")
    def batch_learning_plans() -> dict[str, object]:
        return _envelope(
            {
                "warnings": BATCH_LEARNING_WARNINGS,
                "items": tuple(
                    {
                        "plan": plan,
                        "evidence_windows": batch_learning_repository.evidence_windows(
                            plan.plan_id
                        ),
                        "cycles": batch_learning_repository.cycles(plan.plan_id),
                        "cycle_details": tuple(
                            _batch_cycle_detail(cycle)
                            for cycle in batch_learning_repository.cycles(plan.plan_id)
                        ),
                    }
                    for plan in batch_learning_repository.plans()
                ),
                "automatic_deployment": False,
                "automatic_champion_replacement": False,
                "authority": "NONE",
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.post(
        "/api/v1/strategy-lab/learning-plans",
        dependencies=[Depends(_mutation_guard)],
    )
    def create_batch_learning_plan(
        body: BatchLearningPlanRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        runtime = BatchLearningRuntime.open()
        try:
            value = runtime.create_plan(
                name=body.name,
                source_scope=body.source_scope,
                feature_schema_id=body.feature_schema_id,
                search_template=body.search_template,
                minimum_new_groups=body.minimum_new_groups,
                minimum_new_observations=body.minimum_new_observations,
                maximum_trials_per_cycle=body.maximum_trials_per_cycle,
                maximum_cycles=body.maximum_cycles,
                reason=body.reason,
                idempotency_key=idempotency_key,
            )
            return _envelope(
                {"plan": value, "warnings": BATCH_LEARNING_WARNINGS},
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except (BatchLearningError, ValueError) as error:
            raise _http_error(request, 422, "BATCH_LEARNING_PLAN_REJECTED", str(error)) from error
        finally:
            runtime.close()

    @app.get("/api/v1/strategy-lab/learning-plans/{plan_id}")
    def batch_learning_plan_detail(plan_id: str, request: Request) -> dict[str, object]:
        plan = batch_learning_repository.plan(plan_id)
        if plan is None:
            raise _http_error(request, 404, "BATCH_LEARNING_PLAN_NOT_FOUND", "plan was not found")
        cycles = batch_learning_repository.cycles(plan_id)
        return _envelope(
            {
                "warnings": BATCH_LEARNING_WARNINGS,
                "plan": plan,
                "evidence_windows": batch_learning_repository.evidence_windows(plan_id),
                "cycles": cycles,
                "cycle_details": tuple(_batch_cycle_detail(cycle) for cycle in cycles),
                "history": tuple(
                    decision
                    for cycle in cycles
                    for decision in batch_learning_repository.decisions(cycle.cycle_id)
                ),
                "authority": "NONE",
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.post(
        "/api/v1/strategy-lab/learning-plans/{plan_id}/activate",
        dependencies=[Depends(_mutation_guard)],
    )
    def activate_batch_learning_plan(
        plan_id: str,
        body: BatchLearningActivationRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            plan = BatchLearningLifecycle(batch_learning_repository).activate(
                plan_id=plan_id,
                confirm_plan_id=body.confirm_plan_id,
                confirm_plan_hash=body.confirm_plan_hash,
                opened_at=datetime.now(tz=UTC),
            )
            return _envelope(plan, capability="RESEARCH_EVIDENCE_ONLY")
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_LEARNING_ACTIVATION_REJECTED", str(error)
            ) from error

    def _batch_plan_control(
        plan_id: str,
        action: str,
        body: BatchLearningReasonRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            plan = BatchLearningLifecycle(batch_learning_repository).control(
                plan_id=plan_id,
                action=action,
                reason=body.reason,
                at=datetime.now(tz=UTC),
            )
            return _envelope(plan, capability="RESEARCH_EVIDENCE_ONLY")
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_LEARNING_CONTROL_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/strategy-lab/learning-plans/{plan_id}/pause",
        dependencies=[Depends(_mutation_guard)],
    )
    def pause_batch_learning_plan(
        plan_id: str, body: BatchLearningReasonRequest, request: Request
    ) -> dict[str, object]:
        return _batch_plan_control(plan_id, "pause", body, request)

    @app.post(
        "/api/v1/strategy-lab/learning-plans/{plan_id}/resume",
        dependencies=[Depends(_mutation_guard)],
    )
    def resume_batch_learning_plan(
        plan_id: str, body: BatchLearningReasonRequest, request: Request
    ) -> dict[str, object]:
        return _batch_plan_control(plan_id, "resume", body, request)

    @app.post(
        "/api/v1/strategy-lab/learning-plans/{plan_id}/revoke",
        dependencies=[Depends(_mutation_guard)],
    )
    def revoke_batch_learning_plan(
        plan_id: str, body: BatchLearningReasonRequest, request: Request
    ) -> dict[str, object]:
        return _batch_plan_control(plan_id, "revoke", body, request)

    @app.post(
        "/api/v1/strategy-lab/learning-plans/{plan_id}/trigger",
        dependencies=[Depends(_mutation_guard)],
    )
    def trigger_batch_learning_plan(
        plan_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            result = EvaluateBatchLearningTrigger(batch_learning_repository).execute(
                plan_id=plan_id,
                source=PostgresResolvedEvidenceSource(engine),
                cutoff_at=datetime.now(tz=UTC),
                trigger_type=BatchLearningTriggerType.NEW_RESOLVED_EVIDENCE,
                trigger_evidence={
                    "source": MANAGED_RETAINED_SOURCE_SCOPE,
                    "provider_fetch": False,
                },
                idempotency_key=idempotency_key,
            )
            return _envelope(
                {"window": result[0], "cycle": result[1], "decision": result[2]},
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_LEARNING_TRIGGER_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/strategy-lab/learning-cycles/{cycle_id}/run",
        dependencies=[Depends(_mutation_guard)],
    )
    def run_batch_learning_cycle(cycle_id: str, request: Request) -> dict[str, object]:
        runtime = BatchLearningRuntime.open()
        try:
            cycle, binding, decision = runtime.run_cycle(cycle_id=cycle_id)
            return _envelope(
                {"cycle": cycle, "binding": binding, "decision": decision},
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(request, 409, "BATCH_LEARNING_CYCLE_REJECTED", str(error)) from error
        finally:
            runtime.close()

    @app.post(
        "/api/v1/strategy-lab/learning-cycles/{cycle_id}/recover",
        dependencies=[Depends(_mutation_guard)],
    )
    def recover_batch_learning_cycle(cycle_id: str, request: Request) -> dict[str, object]:
        runtime = BatchLearningRuntime.open()
        try:
            return _envelope(
                runtime.recover(cycle_id=cycle_id),
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_LEARNING_RECOVERY_REJECTED", str(error)
            ) from error
        finally:
            runtime.close()

    @app.post(
        "/api/v1/strategy-lab/learning-cycles/{cycle_id}/cancel",
        dependencies=[Depends(_mutation_guard)],
    )
    def cancel_batch_learning_cycle(
        cycle_id: str,
        body: BatchLearningReasonRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            return _envelope(
                CancelBatchLearningCycle(batch_learning_repository).execute(
                    cycle_id=cycle_id,
                    reason=body.reason,
                    canceled_at=datetime.now(tz=UTC),
                ),
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(request, 409, "BATCH_LEARNING_CANCEL_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/learning-cycles/{cycle_id}/attach-challenger",
        dependencies=[Depends(_mutation_guard)],
    )
    def attach_batch_learning_challenger(
        cycle_id: str,
        body: BatchLearningAttachmentRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        runtime = BatchLearningRuntime.open()
        try:
            cycle, binding, window = runtime.attach(
                cycle_id=cycle_id,
                deployment_id=body.deployment_id,
                idempotency_key=idempotency_key,
            )
            return _envelope(
                {"cycle": cycle, "binding": binding, "forward_window": window},
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_CHALLENGER_ATTACHMENT_REJECTED", str(error)
            ) from error
        finally:
            runtime.close()

    @app.post(
        "/api/v1/strategy-lab/forward-windows/{window_id}/infer",
        dependencies=[Depends(_mutation_guard)],
    )
    def run_batch_forward_inference(
        window_id: str,
        body: BatchLearningCutoffRequest,
        request: Request,
    ) -> dict[str, object]:
        runtime = BatchLearningRuntime.open()
        try:
            return _envelope(
                runtime.infer(window_id=window_id, cutoff_at=body.cutoff_at),
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_FORWARD_INFERENCE_REJECTED", str(error)
            ) from error
        finally:
            runtime.close()

    @app.post(
        "/api/v1/strategy-lab/forward-windows/{window_id}/evaluate",
        dependencies=[Depends(_mutation_guard)],
    )
    def evaluate_batch_forward_window(
        window_id: str,
        body: BatchLearningCutoffRequest,
        request: Request,
    ) -> dict[str, object]:
        runtime = BatchLearningRuntime.open()
        try:
            return _envelope(
                runtime.resolve_and_evaluate(window_id=window_id, cutoff_at=body.cutoff_at),
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except BatchLearningError as error:
            raise _http_error(
                request, 409, "BATCH_FORWARD_EVALUATION_REJECTED", str(error)
            ) from error
        finally:
            runtime.close()

    @app.get("/api/v1/strategy-lab/automated-searches")
    def candidate_searches(experiment_id: str | None = None) -> dict[str, object]:
        plans = candidate_generation_repository.plans(experiment_id)
        return _envelope(
            {
                "warnings": (
                    "AUTOMATED SEARCH EXPLORES ONLY THE PREDECLARED BOUNDED SPACE",
                    "OPTUNA PROPOSES PARAMETERS — APEX OWNS THE TRIAL LEDGER AND DECISION",
                    "MLFLOW IS A LOCAL RESEARCH MIRROR — IT GRANTS NO AUTHORITY",
                    "CANDIDATE ELIGIBILITY DOES NOT GRANT DEPLOYMENT OR CAPITAL AUTHORITY",
                ),
                "items": tuple(
                    {
                        "plan": plan,
                        "runs": candidate_generation_repository.runs(plan.search_plan_id),
                    }
                    for plan in plans
                ),
                "mlflow": {
                    "tracking_mode": "LOCAL_SQLITE_TRACKING",
                    "version": LocalMLflowMirrorAdapter.runtime_version,
                    "bind_host": "127.0.0.1",
                    "loopback_url": None,
                    "loopback_url_reason": "AVAILABLE_ONLY_FROM_HEALTHY_MLFLOW_STATUS",
                    "server_required_for_search": False,
                    "authority": "NONE",
                },
                "deploy_control_present": False,
                "champion_control_present": False,
                "paper_control_present": False,
                "live_control_present": False,
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.post(
        "/api/v1/strategy-lab/automated-searches",
        dependencies=[Depends(_mutation_guard)],
    )
    def create_candidate_search(
        body: CandidateSearchPlanRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            family = research_integrity_repository.family(body.experiment_id)
            dataset = strategy_lab_store.dataset(body.dataset_id)
            if family is None:
                raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
            if dataset is None:
                raise CandidateGenerationApplicationError("STRATEGY_LAB_DATASET_NOT_FOUND")
            if dataset.schema.schema_id != body.feature_schema_id:
                raise CandidateGenerationApplicationError("FEATURE_SCHEMA_ID_MISMATCH")
            maximum_failed = min(5, max(0, body.maximum_trials // 5))
            if maximum_failed >= body.maximum_trials:
                maximum_failed = body.maximum_trials - 1
            plan = CreateAutomatedCandidateSearchPlan(candidate_generation_repository).execute(
                experiment=family,
                existing_experiment_trial_count=len(
                    research_integrity_repository.trials(body.experiment_id)
                ),
                dataset=dataset,
                search_space=initial_strategy_lab_search_space(),
                display_name=body.name,
                permitted_model_families=tuple(ModelFamily),
                objective_identity=body.objective,
                maximum_trials=body.maximum_trials,
                maximum_failed_trials=maximum_failed,
                wall_clock_budget_seconds=3_600,
                per_trial_budget_seconds=300,
                artifact_byte_budget=500_000_000,
                random_seed=body.seed,
                operator_action_id=f"api:{idempotency_key}",
                operator_reason=body.operator_reason,
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
            )
            return _envelope(plan, capability="RESEARCH_EVIDENCE_ONLY")
        except (CandidateGenerationApplicationError, ValueError) as error:
            raise _http_error(request, 422, "CANDIDATE_SEARCH_PLAN_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/automated-searches/{search_plan_id}/runs",
        dependencies=[Depends(_mutation_guard)],
    )
    def run_candidate_search(
        search_plan_id: str,
        body: CandidateSearchRunRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            plan = candidate_generation_repository.plan(search_plan_id)
            if plan is None:
                raise CandidateGenerationApplicationError("SEARCH_PLAN_NOT_FOUND")
            family = research_integrity_repository.family(plan.experiment_id)
            if family is None:
                raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
            workspace = create_local_mlflow_workspace(
                mlflow_version=LocalMLflowMirrorAdapter.runtime_version,
                created_at=datetime.now(tz=UTC),
            )
            workspace = candidate_generation_repository.save_workspace(workspace)
            run, decision, bindings = RunAutomatedCandidateSearch(
                repository=candidate_generation_repository,
                research_ledger=ResearchTrialLedger(research_integrity_repository),
                research_integrity=research_integrity_service,
                optimizer=OptunaCandidateSearchAdapter(
                    plan_hash=plan.canonical_input_hash,
                    seed=plan.random_seed,
                    direction=plan.objective_direction,
                ),
                executor=StrategyLabCandidateTrialExecutor(strategy_lab_service),
                mirror=LocalMLflowMirrorAdapter(),
            ).execute(
                plan=plan,
                experiment=family,
                search_space=initial_strategy_lab_search_space(),
                workspace=workspace,
                requested_trials=body.requested_trials,
                idempotency_key=idempotency_key,
                started_at=datetime.now(tz=UTC),
            )
            return _envelope(
                {"run": run, "decision": decision, "bindings": bindings},
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except (CandidateGenerationApplicationError, ValueError) as error:
            raise _http_error(request, 422, "CANDIDATE_SEARCH_RUN_REJECTED", str(error)) from error

    @app.get("/api/v1/strategy-lab/automated-search-runs/{search_run_id}")
    def candidate_search_detail(search_run_id: str, request: Request) -> dict[str, object]:
        run = candidate_generation_repository.run(search_run_id)
        if run is None:
            raise _http_error(
                request, 404, "CANDIDATE_SEARCH_RUN_NOT_FOUND", "search run was not found"
            )
        return _envelope(
            {
                "run": run,
                "bindings": candidate_generation_repository.bindings(search_run_id),
                "decision": candidate_generation_repository.decision(search_run_id),
                "authority": "NONE",
            },
            capability="RESEARCH_EVIDENCE_ONLY",
        )

    @app.post(
        "/api/v1/strategy-lab/automated-search-runs/{search_run_id}/recover",
        dependencies=[Depends(_mutation_guard)],
    )
    def recover_candidate_search(search_run_id: str, request: Request) -> dict[str, object]:
        try:
            run = candidate_generation_repository.run(search_run_id)
            if run is None:
                raise CandidateGenerationApplicationError("SEARCH_RUN_NOT_FOUND")
            plan = candidate_generation_repository.plan(run.search_plan_id)
            if plan is None:
                raise CandidateGenerationApplicationError("SEARCH_PLAN_NOT_FOUND")
            reasons = RecoverAutomatedCandidateSearch(
                candidate_generation_repository,
                OptunaCandidateSearchAdapter(
                    plan_hash=plan.canonical_input_hash,
                    seed=plan.random_seed,
                    direction=plan.objective_direction,
                ),
            ).execute(search_run_id)
            return _envelope(
                {"search_run_id": search_run_id, "reason_codes": reasons},
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except (CandidateGenerationApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "CANDIDATE_SEARCH_RECOVERY_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/strategy-lab/automated-search-runs/{search_run_id}/cancel",
        dependencies=[Depends(_mutation_guard)],
    )
    def cancel_candidate_search(
        search_run_id: str,
        body: ControlRequest,
        request: Request,
    ) -> dict[str, object]:
        try:
            run = CancelAutomatedCandidateSearch(candidate_generation_repository).execute(
                search_run_id=search_run_id,
                reason=body.operator_reason,
                canceled_at=datetime.now(tz=UTC),
            )
            return _envelope(run, capability="RESEARCH_EVIDENCE_ONLY")
        except CandidateGenerationApplicationError as error:
            raise _http_error(
                request, 422, "CANDIDATE_SEARCH_CANCEL_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/strategy-lab/mlflow-mirror/{experiment_id}/reconcile",
        dependencies=[Depends(_mutation_guard)],
    )
    def reconcile_candidate_mlflow(experiment_id: str, request: Request) -> dict[str, object]:
        try:
            experiment = research_integrity_repository.family(experiment_id)
            if experiment is None:
                raise CandidateGenerationApplicationError("RESEARCH_FAMILY_NOT_FOUND")
            workspace = create_local_mlflow_workspace(
                mlflow_version=LocalMLflowMirrorAdapter.runtime_version,
                created_at=datetime.now(tz=UTC),
            )
            workspace = candidate_generation_repository.save_workspace(workspace)
            reconciliations = reconcile_mlflow_mirror(
                repository=candidate_generation_repository,
                mirror=LocalMLflowMirrorAdapter(),
                experiment=experiment,
                research_trials=research_integrity_repository.trials(experiment_id),
                workspace=workspace,
            )
            return _envelope(
                {
                    "records": candidate_generation_repository.mirror_records(experiment_id),
                    "reconciliations": reconciliations,
                    "authority": "NONE",
                },
                capability="RESEARCH_EVIDENCE_ONLY",
            )
        except (CandidateGenerationApplicationError, ValueError) as error:
            raise _http_error(request, 422, "MLFLOW_RECONCILIATION_REJECTED", str(error)) from error

    @app.get("/api/v1/strategy-lab/feature-schemas")
    def strategy_lab_feature_schemas() -> dict[str, object]:
        return _envelope(
            {
                "warning": "RESEARCH CANDIDATE — NO STRATEGY OR CAPITAL AUTHORITY",
                "items": strategy_lab_store.feature_schemas(),
            }
        )

    @app.get("/api/v1/strategy-lab/label-definitions")
    def strategy_lab_label_definitions() -> dict[str, object]:
        return _envelope({"items": strategy_lab_store.label_definitions()})

    @app.get("/api/v1/strategy-lab/datasets")
    def strategy_lab_datasets() -> dict[str, object]:
        return _envelope({"items": strategy_lab_store.datasets()})

    @app.post("/api/v1/strategy-lab/datasets", dependencies=[Depends(_mutation_guard)])
    def strategy_lab_build_dataset(
        body: StrategyLabDatasetRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            dataset = strategy_lab_service.build_dataset(
                adapter=EventMarketsStrategyLabAdapter(
                    engine,
                    strategy_id=body.strategy_id,
                    strategy_version=body.strategy_version,
                ),
                name=body.name,
                cutoff_at=body.cutoff_at,
                idempotency_key=idempotency_key,
                source_scope=body.configuration_hash,
            )
            return _envelope(dataset)
        except StrategyLabError as error:
            raise _http_error(request, 422, "STRATEGY_LAB_DATASET_REJECTED", str(error)) from error

    @app.get("/api/v1/strategy-lab/datasets/{dataset_id}")
    def strategy_lab_dataset(dataset_id: str, request: Request) -> dict[str, object]:
        dataset = strategy_lab_store.dataset(dataset_id)
        if dataset is None:
            raise _http_error(
                request, 404, "STRATEGY_LAB_DATASET_NOT_FOUND", "dataset was not found"
            )
        return _envelope(dataset)

    @app.get("/api/v1/strategy-lab/training-runs")
    def strategy_lab_training_runs() -> dict[str, object]:
        return _envelope({"items": strategy_lab_store.training_runs()})

    @app.post("/api/v1/strategy-lab/training-runs", dependencies=[Depends(_mutation_guard)])
    def strategy_lab_train(
        body: StrategyLabTrainingRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_service.train_model(
                    dataset_id=body.dataset_id,
                    family=body.model_family,
                    hyperparameters=cast(dict[str, object], body.hyperparameters),
                    calibration=body.calibration,
                    seed=body.seed,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "STRATEGY_LAB_TRAINING_REJECTED", str(error)) from error

    @app.get("/api/v1/strategy-lab/training-runs/{training_run_id}")
    def strategy_lab_training_run(training_run_id: str, request: Request) -> dict[str, object]:
        run = strategy_lab_store.training(training_run_id)
        if run is None:
            raise _http_error(
                request, 404, "STRATEGY_LAB_TRAINING_NOT_FOUND", "training run was not found"
            )
        return _envelope(run)

    @app.post(
        "/api/v1/strategy-lab/training-runs/{training_run_id}/evaluate",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_evaluate(training_run_id: str, request: Request) -> dict[str, object]:
        try:
            evaluation, candidate = strategy_lab_service.evaluate_candidate(
                training_run_id=training_run_id
            )
            return _envelope({"evaluation": evaluation, "candidate": candidate})
        except StrategyLabError as error:
            raise _http_error(
                request, 422, "STRATEGY_LAB_EVALUATION_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/strategy-lab/candidates")
    def strategy_lab_candidates() -> dict[str, object]:
        return _envelope({"items": strategy_lab_store.candidates()})

    @app.get("/api/v1/strategy-lab/candidates/{candidate_id}")
    def strategy_lab_candidate(candidate_id: str, request: Request) -> dict[str, object]:
        try:
            return _envelope(strategy_lab_service.candidate_report(candidate_id))
        except StrategyLabError as error:
            raise _http_error(
                request, 404, "STRATEGY_LAB_CANDIDATE_NOT_FOUND", str(error)
            ) from error

    @app.get("/api/v1/strategy-lab/shadow-reviews")
    def strategy_lab_shadow_reviews() -> dict[str, object]:
        return _envelope(
            {
                "warning": "SHADOW MODEL — NO CAPITAL AUTHORITY",
                "items": strategy_lab_shadow_store.reviews(),
            }
        )

    @app.post(
        "/api/v1/strategy-lab/candidates/{candidate_id}/shadow-reviews",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_request_shadow_review(
        candidate_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.request_review(
                    candidate_id=candidate_id,
                    reason=body.reason,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_REVIEW_REJECTED", str(error)) from error

    @app.get("/api/v1/strategy-lab/shadow-reviews/{review_id}")
    def strategy_lab_shadow_review(review_id: str, request: Request) -> dict[str, object]:
        review = strategy_lab_shadow_store.review(review_id)
        if review is None:
            raise _http_error(request, 404, "SHADOW_REVIEW_NOT_FOUND", "review was not found")
        return _envelope(review)

    @app.post(
        "/api/v1/strategy-lab/shadow-reviews/{review_id}/start",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_start_shadow_review(
        review_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.start_review(
                    review_id=review_id, reason=body.reason, idempotency_key=idempotency_key
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_REVIEW_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/shadow-reviews/{review_id}/approve",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_approve_shadow_review(
        review_id: str,
        body: StrategyLabShadowApprovalRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.approve_review(
                    review_id=review_id,
                    role=body.role,
                    candidate_id=body.confirm_candidate,
                    dataset_id=body.confirm_dataset,
                    artifact_sha256=body.confirm_artifact_sha256,
                    evaluation_id=body.confirm_evaluation,
                    reason=body.reason,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_DEPLOYMENT_REJECTED", str(error)) from error

    def _shadow_review_decision(
        review_id: str,
        body: StrategyLabShadowDecisionRequest,
        request: Request,
        idempotency_key: str,
        state: ShadowReviewState,
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.decide_review(
                    review_id=review_id,
                    state=state,
                    reason=body.reason,
                    note=body.note,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_REVIEW_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/shadow-reviews/{review_id}/reject",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_reject_shadow_review(
        review_id: str,
        body: StrategyLabShadowDecisionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _shadow_review_decision(
            review_id, body, request, idempotency_key, ShadowReviewState.REJECTED
        )

    @app.post(
        "/api/v1/strategy-lab/shadow-reviews/{review_id}/defer",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_defer_shadow_review(
        review_id: str,
        body: StrategyLabShadowDecisionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _shadow_review_decision(
            review_id, body, request, idempotency_key, ShadowReviewState.DEFERRED
        )

    @app.post(
        "/api/v1/strategy-lab/shadow-reviews/{review_id}/cancel",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_cancel_shadow_review(
        review_id: str,
        body: StrategyLabShadowDecisionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _shadow_review_decision(
            review_id, body, request, idempotency_key, ShadowReviewState.CANCELED
        )

    @app.get("/api/v1/strategy-lab/deployments")
    def strategy_lab_deployments() -> dict[str, object]:
        return _envelope(
            {
                "warning": "SHADOW MODEL — NO CAPITAL AUTHORITY",
                "items": strategy_lab_shadow_store.deployments(),
            }
        )

    @app.get("/api/v1/strategy-lab/deployments/{deployment_id}")
    def strategy_lab_deployment(deployment_id: str, request: Request) -> dict[str, object]:
        deployment = strategy_lab_shadow_store.deployment(deployment_id)
        if deployment is None:
            raise _http_error(
                request, 404, "SHADOW_DEPLOYMENT_NOT_FOUND", "deployment was not found"
            )
        return _envelope(deployment)

    def _deployment_control(
        deployment_id: str,
        action: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.control_deployment(
                    deployment_id=deployment_id,
                    action=action,
                    reason=body.reason,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_DEPLOYMENT_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/pause",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_pause_deployment(
        deployment_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _deployment_control(deployment_id, "pause", body, request, idempotency_key)

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/resume",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_resume_deployment(
        deployment_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _deployment_control(deployment_id, "resume", body, request, idempotency_key)

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/revoke",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_revoke_deployment(
        deployment_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _deployment_control(deployment_id, "revoke", body, request, idempotency_key)

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/retire",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_retire_deployment(
        deployment_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _deployment_control(deployment_id, "retire", body, request, idempotency_key)

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/inference-batches",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_run_shadow_inference(
        deployment_id: str,
        body: StrategyLabShadowBatchRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.run_inference(
                    deployment_id=deployment_id,
                    adapter=EventMarketsForwardFeatureAdapter(engine),
                    cutoff_at=body.cutoff_at,
                    maximum_rows=body.maximum_rows,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_INFERENCE_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/resolve",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_resolve_shadow(
        deployment_id: str,
        body: StrategyLabShadowCutoffRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                {
                    "resolved": strategy_lab_shadow_service.resolve_predictions(
                        deployment_id=deployment_id,
                        adapter=EventMarketsForwardFeatureAdapter(engine),
                        cutoff_at=body.cutoff_at,
                    )
                }
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "SHADOW_RESOLUTION_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/deployments/{deployment_id}/evaluate",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_evaluate_challenger(
        deployment_id: str,
        body: StrategyLabShadowCutoffRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.evaluate_challenger(
                    deployment_id=deployment_id, cutoff_at=body.cutoff_at
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "CHALLENGER_EVALUATION_REJECTED", str(error)) from error

    @app.get("/api/v1/strategy-lab/champions")
    def strategy_lab_champions() -> dict[str, object]:
        return _envelope(
            {
                "warning": "CHAMPION STATUS IS RESEARCH COMPARISON, NOT TRADING AUTHORITY",
                "items": strategy_lab_shadow_store.champions(),
            }
        )

    @app.post("/api/v1/strategy-lab/champions", dependencies=[Depends(_mutation_guard)])
    def strategy_lab_assign_champion(
        body: StrategyLabChampionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.assign_champion(
                    deployment_id=body.confirm_deployment,
                    reason=body.reason,
                    confirm_deployment=body.confirm_deployment,
                    idempotency_key=idempotency_key,
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "CHAMPION_ASSIGNMENT_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-lab/champions/{assignment_id}/revoke",
        dependencies=[Depends(_mutation_guard)],
    )
    def strategy_lab_revoke_champion(
        assignment_id: str,
        body: StrategyLabShadowReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                strategy_lab_shadow_service.revoke_champion(
                    assignment_id=assignment_id, reason=body.reason, idempotency_key=idempotency_key
                )
            )
        except StrategyLabError as error:
            raise _http_error(request, 422, "CHAMPION_REVOCATION_REJECTED", str(error)) from error

    @app.get("/api/v1/securities/instruments")
    def securities_instruments() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": SECURITIES_SHADOW_WARNING,
                "items": securities_repository.list_instruments(),
            }
        )

    @app.get("/api/v1/securities/real-data/provider")
    def securities_real_data_provider() -> dict[str, object]:
        configuration = real_securities_repository.active_configuration()
        capability = (
            None
            if configuration is None
            else real_securities_repository.latest_capability(configuration.configuration_id)
        )
        operations = real_securities_repository.list_operations()
        return _securities_envelope(
            {
                "warnings": (
                    "REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE",
                    "IEX DATA IS SINGLE-VENUE COVERAGE, NOT THE FULL CONSOLIDATED MARKET",
                    "HISTORICAL RESULTS DO NOT GRANT PAPER OR LIVE AUTHORITY",
                ),
                "configuration": configuration,
                "credential_availability_only": (
                    None if capability is None else capability.credentials_available
                ),
                "latest_capability": capability,
                "latest_operation": operations[-1] if operations else None,
                "financial_authority": False,
                "brokerage_endpoint_calls": 0,
            }
        )

    @app.get("/api/v1/securities/real-data/universes")
    def securities_real_data_universes() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE",
                "items": real_securities_repository.list_universes(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/real-data/fetch-operations")
    def securities_real_data_fetch_operations() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE",
                "items": real_securities_repository.list_operations(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/real-data/quality")
    def securities_real_data_quality() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE",
                "items": real_securities_repository.list_quality(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/real-data/historical-validations")
    def securities_real_data_historical_validations() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "HISTORICAL RESULTS DO NOT GRANT PAPER OR LIVE AUTHORITY",
                "items": real_securities_repository.list_validations(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/real-data/forward-plans")
    def securities_real_data_forward_plans() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "FORWARD EVIDENCE REMAINS RESEARCH-ONLY",
                "items": real_securities_repository.list_forward_plans(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-data/plans")
    def securities_historical_data_plans() -> dict[str, object]:
        from apex.application.historical_market_data import (
            historical_market_data_warnings,
            plan_summary_state,
        )

        plans = historical_market_data_repository.list_plans()
        return _securities_envelope(
            {
                "warnings": historical_market_data_warnings(),
                "items": tuple(
                    {
                        "plan": plan,
                        "summary_state": plan_summary_state(
                            repository=historical_market_data_repository, plan=plan
                        ).value,
                        "chunks": historical_market_data_repository.list_chunks(plan.plan_id),
                    }
                    for plan in plans
                ),
                "financial_authority": False,
                "brokerage_endpoint_calls": 0,
            }
        )

    @app.get("/api/v1/securities/historical-data/snapshots")
    def securities_historical_data_snapshots() -> dict[str, object]:
        from apex.application.historical_market_data import historical_market_data_warnings

        return _securities_envelope(
            {
                "warnings": historical_market_data_warnings(),
                "items": historical_market_data_repository.list_snapshots(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-data/quality-reports")
    def securities_historical_data_quality_reports() -> dict[str, object]:
        from apex.application.historical_market_data import historical_market_data_warnings

        return _securities_envelope(
            {
                "warnings": historical_market_data_warnings(),
                "items": historical_market_data_repository.list_quality_reports(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-replay/runs")
    def historical_replay_runs() -> dict[str, object]:
        """Bounded run inventory -- every run's declared bindings, cursor,
        and completion state; never the frames/evaluations themselves (see
        the per-run endpoints below for those, each independently bounded)."""

        from apex.application.historical_replay import historical_replay_warnings

        runs = historical_replay_repository.list_runs()
        return _securities_envelope(
            {
                "warnings": historical_replay_warnings(),
                "items": tuple(
                    {
                        "run": run,
                        "checkpoint_count": len(
                            historical_replay_repository.list_checkpoints(run.replay_run_id)
                        ),
                    }
                    for run in runs
                ),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-replay/runs/{replay_run_id}")
    def historical_replay_run_inspect(replay_run_id: str) -> dict[str, object]:
        from apex.application.historical_replay import historical_replay_warnings

        run = historical_replay_repository.get_run(replay_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="REPLAY_RUN_NOT_FOUND")
        checkpoints = historical_replay_repository.list_checkpoints(replay_run_id)
        return _securities_envelope(
            {
                "warnings": historical_replay_warnings(),
                "run": run,
                "checkpoint_count": len(checkpoints),
                "latest_checkpoint": checkpoints[-1] if checkpoints else None,
                "research_period_policy": historical_replay_repository.research_period_policy(
                    replay_run_id
                ),
                "experiment_links": historical_replay_repository.experiment_links(replay_run_id),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-replay/runs/{replay_run_id}/frames")
    def historical_replay_run_frames(replay_run_id: str, limit: int = 100) -> dict[str, object]:
        from apex.application.historical_replay import (
            MAXIMUM_LIST_RESULTS,
            historical_replay_warnings,
        )

        bounded_limit = max(1, min(limit, MAXIMUM_LIST_RESULTS))
        return _securities_envelope(
            {
                "warnings": historical_replay_warnings(),
                "replay_run_id": replay_run_id,
                "items": historical_replay_repository.list_frames(
                    replay_run_id, limit=bounded_limit
                ),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-replay/runs/{replay_run_id}/evaluations")
    def historical_replay_run_evaluations(
        replay_run_id: str, limit: int = 100
    ) -> dict[str, object]:
        from apex.application.historical_replay import (
            MAXIMUM_LIST_RESULTS,
            historical_replay_warnings,
        )

        bounded_limit = max(1, min(limit, MAXIMUM_LIST_RESULTS))
        return _securities_envelope(
            {
                "warnings": historical_replay_warnings(),
                "replay_run_id": replay_run_id,
                "items": historical_replay_repository.list_evaluations(
                    replay_run_id, limit=bounded_limit
                ),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/historical-replay/runs/{replay_run_id}/summary")
    def historical_replay_run_summary(replay_run_id: str) -> dict[str, object]:
        from apex.application.historical_replay import (
            HistoricalReplayNotFound,
            build_historical_research_summary,
            historical_replay_warnings,
        )

        try:
            summary = build_historical_research_summary(
                repository=historical_replay_repository, replay_run_id=replay_run_id
            )
        except HistoricalReplayNotFound as error:
            raise HTTPException(status_code=404, detail="REPLAY_RUN_NOT_FOUND") from error
        return _securities_envelope(
            {
                "warnings": historical_replay_warnings(),
                "summary": summary,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/policies/{governance_policy_id}")
    def research_methodology_policy_inspect(governance_policy_id: str) -> dict[str, object]:
        from apex.domain.research_methodology import research_methodology_warnings

        policy = research_methodology_repository.governance_policy_by_id(governance_policy_id)
        if policy is None:
            raise HTTPException(status_code=404, detail="GOVERNANCE_POLICY_NOT_FOUND")
        exposures = research_methodology_repository.holdout_exposures_for_policy(
            governance_policy_id
        )
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "governance_policy": policy,
                "holdout_exposure_count": len(exposures),
                "holdout_exposures": exposures,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/trials/{trial_id}")
    def research_methodology_trial_inspect(trial_id: str) -> dict[str, object]:
        from apex.domain.research_methodology import research_methodology_warnings

        trial = research_methodology_repository.trial_by_id(trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail="RESEARCH_TRIAL_NOT_FOUND")
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "trial": trial,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/trial-families/{trial_family_id}")
    def research_methodology_trial_family_inspect(trial_family_id: str) -> dict[str, object]:
        from apex.domain.research_methodology import research_methodology_warnings

        family = research_methodology_repository.trial_family_by_id(trial_family_id)
        if family is None:
            raise HTTPException(status_code=404, detail="TRIAL_FAMILY_NOT_FOUND")
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "trial_family": family,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/walk-forward/{walk_forward_run_id}")
    def research_methodology_walk_forward_inspect(walk_forward_run_id: str) -> dict[str, object]:
        from apex.application.research_methodology import build_walk_forward_summary
        from apex.domain.research_methodology import research_methodology_warnings

        run = research_methodology_repository.get_walk_forward_run(walk_forward_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="WALK_FORWARD_RUN_NOT_FOUND")
        folds = research_methodology_repository.folds_for_run(walk_forward_run_id)
        summary = None
        if folds:
            try:
                summary = build_walk_forward_summary(
                    repository=research_methodology_repository,
                    walk_forward_run_id=walk_forward_run_id,
                )
            except Exception:
                summary = None
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "walk_forward_run": run,
                "fold_count": len(folds),
                "folds": folds,
                "summary": summary,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/robustness/{robustness_run_id}")
    def research_methodology_robustness_inspect(robustness_run_id: str) -> dict[str, object]:
        from apex.domain.research_methodology import research_methodology_warnings

        robustness = research_methodology_repository.robustness_run_by_id(robustness_run_id)
        if robustness is None:
            raise HTTPException(status_code=404, detail="ROBUSTNESS_CHECK_RUN_NOT_FOUND")
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "robustness_check_run": robustness,
                "financial_authority": False,
            }
        )

    @app.get(
        "/api/v1/securities/research-methodology/conclusions/by-hypothesis/{hypothesis_version_id}"
    )
    def research_methodology_conclusions_for_hypothesis(
        hypothesis_version_id: str,
    ) -> dict[str, object]:
        from apex.domain.research_methodology import research_methodology_warnings

        conclusions = research_methodology_repository.conclusions_for_hypothesis(
            hypothesis_version_id
        )
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "hypothesis_version_id": hypothesis_version_id,
                "conclusion_count": len(conclusions),
                "conclusions": conclusions,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/conclusions/{conclusion_id}")
    def research_methodology_conclusion_inspect(conclusion_id: str) -> dict[str, object]:
        from apex.domain.research_methodology import research_methodology_warnings

        conclusion = research_methodology_repository.conclusion_by_id(conclusion_id)
        if conclusion is None:
            raise HTTPException(status_code=404, detail="RESEARCH_CONCLUSION_NOT_FOUND")
        promotions = research_methodology_repository.promotion_evaluations_for_conclusion(
            conclusion_id
        )
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "conclusion": conclusion,
                "promotion_evaluations": promotions,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-methodology/audit/{hypothesis_version_id}")
    def research_methodology_audit_inspect(hypothesis_version_id: str) -> dict[str, object]:
        from apex.application.research_methodology import build_research_audit_trail
        from apex.domain.research_methodology import research_methodology_warnings

        trail = build_research_audit_trail(
            repository=research_methodology_repository,
            hypothesis_version_id=hypothesis_version_id,
        )
        return _securities_envelope(
            {
                "warnings": research_methodology_warnings(),
                "audit_trail": trail,
                "financial_authority": False,
            }
        )

    def _cross_market_research_envelope(value: object) -> dict[str, object]:
        return _securities_envelope(value)

    @app.get("/api/v1/securities/cross-market-research/agendas")
    def cross_market_research_agendas() -> dict[str, object]:
        from apex.domain.cross_market_research import cross_market_research_warnings

        return _cross_market_research_envelope(
            {
                "warnings": cross_market_research_warnings(),
                "items": cross_market_research_repository.list_agendas(),
                "financial_authority": False,
            }
        )

    class CrossMarketResearchAgendaCreateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        dataset_snapshot_id: str = Field(min_length=1, max_length=160)
        replay_run_id: str = Field(min_length=1, max_length=160)
        cross_market_dataset_id: str = Field(min_length=1, max_length=160)
        cohort_policy_id: str = Field(min_length=1, max_length=160)
        created_at: datetime

    @app.post("/api/v1/securities/cross-market-research/agendas")
    def cross_market_research_agenda_create(
        request: CrossMarketResearchAgendaCreateRequest,
    ) -> dict[str, object]:
        from apex.application.cross_market_research import build_v1_agenda

        agenda = build_v1_agenda(
            dataset_snapshot_id=request.dataset_snapshot_id,
            replay_run_id=request.replay_run_id,
            cross_market_dataset_id=request.cross_market_dataset_id,
            cohort_policy_id=request.cohort_policy_id,
            registered_at=request.created_at,
        )
        existing = cross_market_research_repository.agenda_by_id(agenda.agenda_id)
        saved = cross_market_research_repository.save_agenda(agenda)
        return _cross_market_research_envelope(
            {
                "agenda": saved,
                "created_evidence": () if existing else (saved.agenda_id,),
                "reused_evidence": (saved.agenda_id,) if existing else (),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/cross-market-research/agendas/{agenda_id}")
    def cross_market_research_agenda_inspect(agenda_id: str) -> dict[str, object]:
        agenda = cross_market_research_repository.agenda_by_id(agenda_id)
        if agenda is None:
            raise HTTPException(status_code=404, detail="RESEARCH_AGENDA_NOT_FOUND")
        return _cross_market_research_envelope(
            {
                "agenda": agenda,
                "hypotheses": cross_market_research_repository.hypotheses_for_agenda(agenda_id),
                "agenda_run": cross_market_research_repository.agenda_run_by_agenda(agenda_id),
                "quality_report": cross_market_research_repository.quality_report_by_agenda(
                    agenda_id
                ),
                "financial_authority": False,
            }
        )

    class CrossMarketResearchGenerateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        created_at: datetime

    @app.post("/api/v1/securities/cross-market-research/agendas/{agenda_id}/hypotheses/generate")
    def cross_market_research_hypotheses_generate(
        agenda_id: str, request: CrossMarketResearchGenerateRequest
    ) -> dict[str, object]:
        from apex.application.cross_market_research import generate_v1_agenda_hypotheses

        agenda = cross_market_research_repository.agenda_by_id(agenda_id)
        if agenda is None:
            raise HTTPException(status_code=404, detail="RESEARCH_AGENDA_NOT_FOUND")
        hypotheses = generate_v1_agenda_hypotheses(agenda=agenda, created_at=request.created_at)
        created: list[str] = []
        reused: list[str] = []
        for hypothesis in hypotheses:
            existing_hypothesis = cross_market_research_repository.hypothesis_by_id(
                hypothesis.hypothesis_id
            )
            cross_market_research_repository.save_hypothesis(hypothesis)
            (reused if existing_hypothesis else created).append(hypothesis.hypothesis_id)
        return _cross_market_research_envelope(
            {
                "agenda_id": agenda_id,
                "hypotheses": hypotheses,
                "created_evidence": tuple(created),
                "reused_evidence": tuple(reused),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/cross-market-research/agendas/{agenda_id}/hypotheses")
    def cross_market_research_hypotheses_list(agenda_id: str) -> dict[str, object]:
        hypotheses = cross_market_research_repository.hypotheses_for_agenda(agenda_id)
        families: dict[str, int] = {}
        for item in hypotheses:
            families[item.family.value] = families.get(item.family.value, 0) + 1
        return _cross_market_research_envelope(
            {
                "agenda_id": agenda_id,
                "items": hypotheses,
                "families": tuple(sorted(families.items())),
                "negative_control_count": sum(1 for item in hypotheses if item.is_negative_control),
                "financial_authority": False,
            }
        )

    class CrossMarketResearchRunStepRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        train_end: str = Field(min_length=10, max_length=10)
        validation_end: str = Field(min_length=10, max_length=10)
        maximum_hypotheses: int = Field(default=25, ge=1, le=30)
        created_at: datetime

    def _run_agenda_step(
        *, agenda_id: str, request: CrossMarketResearchRunStepRequest, start: bool
    ) -> dict[str, object]:
        from apex.application.cross_market_research import (
            AdvanceAgendaRun,
            BuildCrossMarketPanel,
            RegisterAgendaRun,
            build_agenda_timeline,
        )

        agenda = cross_market_research_repository.agenda_by_id(agenda_id)
        if agenda is None:
            raise HTTPException(status_code=404, detail="RESEARCH_AGENDA_NOT_FOUND")
        hypotheses = cross_market_research_repository.hypotheses_for_agenda(agenda_id)
        if not hypotheses:
            raise HTTPException(status_code=409, detail="NO_HYPOTHESES_GENERATED_FOR_AGENDA_YET")
        panel = BuildCrossMarketPanel(
            historical_repository=historical_market_data_repository,
            evidence=securities_repository,
            genuine_repository=genuine_multi_asset_repository,
            crypto_repository=crypto_research_evidence_repository,
        ).execute(
            equity_snapshot_id=agenda.dataset_snapshot_id,
            cross_market_dataset_id=agenda.cross_market_dataset_id,
            horizons=agenda.allowed_horizons_sessions,
        )
        timeline = build_agenda_timeline(
            panel=panel,
            agenda=agenda,
            train_end=request.train_end,
            validation_end=request.validation_end,
            created_at=request.created_at,
        )
        if start:
            RegisterAgendaRun(repository=cross_market_research_repository).execute(
                agenda=agenda, hypotheses=hypotheses, created_at=request.created_at
            )
        run = AdvanceAgendaRun(
            repository=cross_market_research_repository,
            methodology_repository=research_methodology_repository,
            timeline=timeline,
            panel_warnings=panel.warnings,
        ).execute(
            agenda=agenda,
            panel=panel,
            maximum_hypotheses=request.maximum_hypotheses,
            created_at=request.created_at,
        )
        return _cross_market_research_envelope(
            {
                "agenda_run": run,
                "panel_warnings": panel.warnings,
                "financial_authority": False,
            }
        )

    @app.post("/api/v1/securities/cross-market-research/agendas/{agenda_id}/run/start")
    def cross_market_research_run_start(
        agenda_id: str, request: CrossMarketResearchRunStepRequest
    ) -> dict[str, object]:
        return _run_agenda_step(agenda_id=agenda_id, request=request, start=True)

    @app.post("/api/v1/securities/cross-market-research/agendas/{agenda_id}/run/resume")
    def cross_market_research_run_resume(
        agenda_id: str, request: CrossMarketResearchRunStepRequest
    ) -> dict[str, object]:
        return _run_agenda_step(agenda_id=agenda_id, request=request, start=False)

    @app.get("/api/v1/securities/cross-market-research/agendas/{agenda_id}/run")
    def cross_market_research_run_inspect(agenda_id: str) -> dict[str, object]:
        run = cross_market_research_repository.agenda_run_by_agenda(agenda_id)
        if run is None:
            raise HTTPException(status_code=404, detail="AGENDA_RUN_NOT_FOUND")
        executions = cross_market_research_repository.executions_for_agenda(agenda_id)
        return _cross_market_research_envelope(
            {
                "agenda_run": run,
                "executions_completed": len(executions),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/cross-market-research/hypotheses/{hypothesis_id}/knowledge")
    def cross_market_research_knowledge_inspect(hypothesis_id: str) -> dict[str, object]:
        history = cross_market_research_repository.knowledge_entries_for_hypothesis(hypothesis_id)
        if not history:
            raise HTTPException(status_code=404, detail="RESEARCH_KNOWLEDGE_NOT_FOUND")
        return _cross_market_research_envelope(
            {
                "hypothesis_id": hypothesis_id,
                "knowledge_history": history,
                "latest": history[-1],
                "execution": cross_market_research_repository.execution_by_hypothesis(
                    hypothesis_id
                ),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/cross-market-research/contradictions")
    def cross_market_research_contradictions() -> dict[str, object]:
        return _cross_market_research_envelope(
            {
                "items": cross_market_research_repository.list_contradictions(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/cross-market-research/follow-up-proposals")
    def cross_market_research_follow_up_proposals() -> dict[str, object]:
        return _cross_market_research_envelope(
            {
                "items": cross_market_research_repository.list_follow_ups(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/cross-market-research/agendas/{agenda_id}/quality-report")
    def cross_market_research_quality_report(agenda_id: str) -> dict[str, object]:
        report = cross_market_research_repository.quality_report_by_agenda(agenda_id)
        if report is None:
            raise HTTPException(status_code=404, detail="RESEARCH_AGENDA_QUALITY_REPORT_NOT_FOUND")
        return _cross_market_research_envelope(
            {"quality_report": report, "financial_authority": False}
        )

    # -- Research Signal Calibration and Negative-Control Hardening v1 ------
    # Extends the existing cross-market-research surface above rather than
    # creating a new dashboard: same "/api/v1/securities/..." namespace, same
    # envelope helper, same authority-NONE convention. Nothing here creates a
    # Strategy, Shared Opportunity, PAPER order, or LIVE authority, and no
    # route here claims trading edge or alpha.

    def _research_calibration_envelope(value: object) -> dict[str, object]:
        return _securities_envelope(value)

    @app.get("/api/v1/securities/research-calibration/plans")
    def research_calibration_plans() -> dict[str, object]:
        from apex.domain.research_calibration import research_calibration_warnings

        return _research_calibration_envelope(
            {
                "warnings": research_calibration_warnings(),
                "items": research_calibration_repository.list_plans(),
                "financial_authority": False,
            }
        )

    class ResearchCalibrationCreateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        dataset_snapshot_id: str = Field(min_length=1, max_length=160)
        replay_run_id: str = Field(min_length=1, max_length=160)
        cross_market_dataset_id: str = Field(min_length=1, max_length=160)
        source_agenda_id: str = Field(min_length=1, max_length=160)
        created_at: datetime

    @app.post("/api/v1/securities/research-calibration/plans")
    def research_calibration_create(request: ResearchCalibrationCreateRequest) -> dict[str, object]:
        from apex.application.research_calibration import (
            build_v1_calibration_plan,
            generate_v1_null_control_specs,
        )

        source_agenda = cross_market_research_repository.agenda_by_id(request.source_agenda_id)
        if source_agenda is None:
            raise HTTPException(status_code=404, detail="SOURCE_RESEARCH_AGENDA_NOT_FOUND")
        plan = build_v1_calibration_plan(
            dataset_snapshot_id=request.dataset_snapshot_id,
            replay_run_id=request.replay_run_id,
            cross_market_dataset_id=request.cross_market_dataset_id,
            source_agenda_id=request.source_agenda_id,
            registered_at=request.created_at,
        )
        existing_plan = research_calibration_repository.plan_by_id(plan.calibration_id)
        saved_plan = research_calibration_repository.save_plan(plan)
        specs = generate_v1_null_control_specs(
            calibration=saved_plan, created_at=request.created_at
        )
        created: list[str] = []
        reused: list[str] = []
        for spec in specs:
            existing_spec = research_calibration_repository.spec_by_id(spec.spec_id)
            research_calibration_repository.save_spec(spec)
            (reused if existing_spec else created).append(spec.spec_id)
        return _research_calibration_envelope(
            {
                "calibration": saved_plan,
                "null_control_count": len(specs),
                "created_evidence": () if existing_plan else tuple(created),
                "reused_evidence": tuple(reused) if existing_plan else (),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-calibration/plans/{calibration_id}")
    def research_calibration_inspect(calibration_id: str) -> dict[str, object]:
        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_PLAN_NOT_FOUND")
        specs = research_calibration_repository.specs_for_calibration(calibration_id)
        families: dict[str, int] = {}
        for spec in specs:
            families[spec.family.value] = families.get(spec.family.value, 0) + 1
        return _research_calibration_envelope(
            {
                "calibration": plan,
                "null_control_count": len(specs),
                "families": tuple(sorted(families.items())),
                "calibration_run": research_calibration_repository.run_by_calibration(
                    calibration_id
                ),
                "report": research_calibration_repository.report_by_calibration(calibration_id),
                "financial_authority": False,
            }
        )

    class ResearchCalibrationRunStepRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        train_end: str = Field(min_length=10, max_length=10)
        validation_end: str = Field(min_length=10, max_length=10)
        maximum_specs: int = Field(default=104, ge=1, le=150)
        created_at: datetime

    def _run_calibration_step(
        *, calibration_id: str, request: ResearchCalibrationRunStepRequest, start: bool
    ) -> dict[str, object]:
        from apex.application.cross_market_research import (
            BuildCrossMarketPanel,
            build_agenda_timeline,
        )
        from apex.application.research_calibration import (
            AdvanceCalibrationRun,
            RegisterCalibrationRun,
        )

        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_PLAN_NOT_FOUND")
        source_agenda = cross_market_research_repository.agenda_by_id(plan.source_agenda_id)
        if source_agenda is None:
            raise HTTPException(status_code=404, detail="SOURCE_RESEARCH_AGENDA_NOT_FOUND")
        specs = research_calibration_repository.specs_for_calibration(calibration_id)
        if not specs:
            raise HTTPException(
                status_code=409, detail="NO_NULL_CONTROLS_GENERATED_FOR_CALIBRATION_YET"
            )
        panel = BuildCrossMarketPanel(
            historical_repository=historical_market_data_repository,
            evidence=securities_repository,
            genuine_repository=genuine_multi_asset_repository,
            crypto_repository=crypto_research_evidence_repository,
        ).execute(
            equity_snapshot_id=source_agenda.dataset_snapshot_id,
            cross_market_dataset_id=source_agenda.cross_market_dataset_id,
            horizons=source_agenda.allowed_horizons_sessions,
        )
        timeline = build_agenda_timeline(
            panel=panel,
            agenda=source_agenda,
            train_end=request.train_end,
            validation_end=request.validation_end,
            created_at=request.created_at,
        )
        if start:
            RegisterCalibrationRun(repository=research_calibration_repository).execute(
                calibration=plan, specs=specs, created_at=request.created_at
            )
        run = AdvanceCalibrationRun(
            repository=research_calibration_repository,
            methodology_repository=research_methodology_repository,
            timeline=timeline,
        ).execute(
            calibration=plan,
            panel=panel,
            maximum_specs=request.maximum_specs,
            created_at=request.created_at,
        )
        return _research_calibration_envelope(
            {"calibration_run": run, "panel_warnings": panel.warnings, "financial_authority": False}
        )

    @app.post("/api/v1/securities/research-calibration/plans/{calibration_id}/run/start")
    def research_calibration_run_start(
        calibration_id: str, request: ResearchCalibrationRunStepRequest
    ) -> dict[str, object]:
        return _run_calibration_step(calibration_id=calibration_id, request=request, start=True)

    @app.post("/api/v1/securities/research-calibration/plans/{calibration_id}/run/resume")
    def research_calibration_run_resume(
        calibration_id: str, request: ResearchCalibrationRunStepRequest
    ) -> dict[str, object]:
        return _run_calibration_step(calibration_id=calibration_id, request=request, start=False)

    @app.get("/api/v1/securities/research-calibration/plans/{calibration_id}/run")
    def research_calibration_run_inspect(calibration_id: str) -> dict[str, object]:
        run = research_calibration_repository.run_by_calibration(calibration_id)
        if run is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_RUN_NOT_FOUND")
        outcomes = research_calibration_repository.outcomes_for_run(run.calibration_run_id)
        return _research_calibration_envelope(
            {
                "calibration_run": run,
                "outcomes_completed": len(outcomes),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-calibration/plans/{calibration_id}/report")
    def research_calibration_report_inspect(calibration_id: str) -> dict[str, object]:
        report = research_calibration_repository.report_by_calibration(calibration_id)
        if report is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_REPORT_NOT_FOUND")
        method_comparisons = research_calibration_repository.method_comparisons_for_calibration(
            calibration_id
        )
        return _research_calibration_envelope(
            {
                "report": report,
                "breakdowns": research_calibration_repository.breakdowns_for_calibration(
                    calibration_id
                ),
                "method_comparisons": method_comparisons,
                "financial_authority": False,
            }
        )

    @app.get(
        "/api/v1/securities/research-calibration/plans/{calibration_id}"
        "/hypotheses/{hypothesis_id}/evidence-grade"
    )
    def research_calibration_evidence_grade_inspect(
        calibration_id: str, hypothesis_id: str
    ) -> dict[str, object]:
        recalibrations = research_calibration_repository.recalibrations_for_calibration(
            calibration_id
        )
        match = next((item for item in recalibrations if item.hypothesis_id == hypothesis_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="EVIDENCE_GRADE_RECALIBRATION_NOT_FOUND")
        return _research_calibration_envelope(
            {"recalibration": match, "financial_authority": False}
        )

    class ResearchCalibrationRecalibrateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        created_at: datetime

    @app.post("/api/v1/securities/research-calibration/plans/{calibration_id}/recalibrate")
    def research_calibration_recalibrate(
        calibration_id: str, request: ResearchCalibrationRecalibrateRequest
    ) -> dict[str, object]:
        from apex.application.research_calibration import (
            AssessFollowUpPriority,
            RecalibratePriorAgenda,
        )

        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_PLAN_NOT_FOUND")
        run = research_calibration_repository.run_by_calibration(calibration_id)
        if run is None or run.status.value != "COMPLETE":
            raise HTTPException(status_code=409, detail="CALIBRATION_RUN_NOT_COMPLETE")
        null_cohens_d = []
        for outcome in research_calibration_repository.outcomes_for_run(run.calibration_run_id):
            if outcome.validation_trial_id:
                trial = research_methodology_repository.trial_by_id(outcome.validation_trial_id)
                if (
                    trial is not None
                    and trial.diagnostics.effect_size is not None
                    and trial.diagnostics.effect_size.cohens_d is not None
                ):
                    null_cohens_d.append(trial.diagnostics.effect_size.cohens_d)
        recalibrations = RecalibratePriorAgenda(
            cross_market_repository=cross_market_research_repository,
            methodology_repository=research_methodology_repository,
        ).execute(
            calibration=plan,
            source_agenda_id=plan.source_agenda_id,
            null_cohens_d=tuple(null_cohens_d),
            created_at=request.created_at,
        )
        for recalibration_item in recalibrations:
            research_calibration_repository.save_recalibration(recalibration_item)
        assessments = AssessFollowUpPriority(
            cross_market_repository=cross_market_research_repository
        ).execute(calibration=plan, recalibrations=recalibrations, created_at=request.created_at)
        for assessment_item in assessments:
            research_calibration_repository.save_follow_up_priority(assessment_item)
        return _research_calibration_envelope(
            {
                "recalibrations": recalibrations,
                "follow_up_priorities": assessments,
                "null_control_sample": len(null_cohens_d),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-calibration/plans/{calibration_id}/follow-up-priorities")
    def research_calibration_follow_up_priorities(calibration_id: str) -> dict[str, object]:
        return _research_calibration_envelope(
            {
                "items": research_calibration_repository.follow_up_priorities_for_calibration(
                    calibration_id
                ),
                "financial_authority": False,
            }
        )

    # -- Research Calibration v2: Template-Matched Nulls and Stability -----
    # Extends this same "/api/v1/securities/research-calibration/..." surface
    # (a "-v2" plans namespace, same envelope helper, same authority-NONE
    # convention) rather than creating a new dashboard. Nothing here creates
    # a Strategy, Shared Opportunity, PAPER order, or LIVE authority, and no
    # route here ever executes the three currently JUSTIFIED follow-ups --
    # only requalifies them.

    @app.get("/api/v1/securities/research-calibration-v2/plans")
    def research_calibration_v2_plans() -> dict[str, object]:
        from apex.domain.research_calibration_v2 import research_calibration_v2_warnings

        return _research_calibration_envelope(
            {
                "warnings": research_calibration_v2_warnings(),
                "items": research_calibration_repository.list_plans(),
                "financial_authority": False,
            }
        )

    class ResearchCalibrationV2CreateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        source_calibration_id: str = Field(min_length=1, max_length=160)
        created_at: datetime

    @app.post("/api/v1/securities/research-calibration-v2/plans")
    def research_calibration_v2_create(
        request: ResearchCalibrationV2CreateRequest,
    ) -> dict[str, object]:
        from apex.application.research_calibration_v2 import (
            build_v2_calibration_plan,
            generate_v2_null_control_specs,
        )

        source_plan = research_calibration_repository.plan_by_id(request.source_calibration_id)
        if source_plan is None:
            raise HTTPException(status_code=404, detail="SOURCE_V1_CALIBRATION_PLAN_NOT_FOUND")
        draft_plan = build_v2_calibration_plan(
            dataset_snapshot_id=source_plan.dataset_snapshot_id,
            replay_run_id=source_plan.replay_run_id,
            cross_market_dataset_id=source_plan.cross_market_dataset_id,
            source_agenda_id=source_plan.source_agenda_id,
            source_calibration_id=source_plan.calibration_id,
            registered_at=request.created_at,
        )
        existing_plan = research_calibration_repository.plan_by_id(draft_plan.calibration_id)
        saved_plan = research_calibration_repository.save_plan(draft_plan)
        specs = generate_v2_null_control_specs(
            calibration=saved_plan, created_at=request.created_at
        )
        created: list[str] = []
        reused: list[str] = []
        for spec in specs:
            existing_spec = research_calibration_v2_repository.spec_by_id(spec.spec_id)
            research_calibration_v2_repository.save_spec(spec)
            (reused if existing_spec else created).append(spec.spec_id)
        return _research_calibration_envelope(
            {
                "calibration": saved_plan,
                "source_calibration_id": source_plan.calibration_id,
                "null_control_count": len(specs),
                "created_evidence": () if existing_plan else tuple(created),
                "reused_evidence": tuple(reused) if existing_plan else (),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-calibration-v2/plans/{calibration_id}")
    def research_calibration_v2_inspect(calibration_id: str) -> dict[str, object]:
        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_V2_PLAN_NOT_FOUND")
        specs = research_calibration_v2_repository.specs_for_calibration(calibration_id)
        designs: dict[str, int] = {}
        for spec in specs:
            designs[spec.research_design.value] = designs.get(spec.research_design.value, 0) + 1
        return _research_calibration_envelope(
            {
                "calibration": plan,
                "null_control_count": len(specs),
                "designs": tuple(sorted(designs.items())),
                "calibration_run": research_calibration_repository.run_by_calibration(
                    calibration_id
                ),
                "report": research_calibration_repository.report_by_calibration(calibration_id),
                "financial_authority": False,
            }
        )

    class ResearchCalibrationV2RunStepRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        source_calibration_id: str = Field(min_length=1, max_length=160)
        train_end: str = Field(min_length=10, max_length=10)
        validation_end: str = Field(min_length=10, max_length=10)
        maximum_specs: int = Field(default=20, ge=1, le=180)
        created_at: datetime

    def _run_calibration_v2_step(
        *, calibration_id: str, request: ResearchCalibrationV2RunStepRequest, start: bool
    ) -> dict[str, object]:
        from apex.application.cross_market_research import (
            BuildCrossMarketPanel,
            build_agenda_timeline,
        )
        from apex.application.research_calibration_v2 import (
            AdvanceCalibrationRunV2,
            FinalizeCalibrationV2,
            RegisterCalibrationRunV2,
            select_walk_forward_null_subset,
        )
        from apex.cli import _v2_priority_strata

        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_V2_PLAN_NOT_FOUND")
        source_agenda = cross_market_research_repository.agenda_by_id(plan.source_agenda_id)
        if source_agenda is None:
            raise HTTPException(status_code=404, detail="SOURCE_RESEARCH_AGENDA_NOT_FOUND")
        specs = research_calibration_v2_repository.specs_for_calibration(calibration_id)
        if not specs:
            raise HTTPException(
                status_code=409, detail="NO_V2_NULL_CONTROLS_GENERATED_FOR_CALIBRATION_YET"
            )
        panel = BuildCrossMarketPanel(
            historical_repository=historical_market_data_repository,
            evidence=securities_repository,
            genuine_repository=genuine_multi_asset_repository,
            crypto_repository=crypto_research_evidence_repository,
        ).execute(
            equity_snapshot_id=source_agenda.dataset_snapshot_id,
            cross_market_dataset_id=source_agenda.cross_market_dataset_id,
            horizons=source_agenda.allowed_horizons_sessions,
        )
        timeline = build_agenda_timeline(
            panel=panel,
            agenda=source_agenda,
            train_end=request.train_end,
            validation_end=request.validation_end,
            created_at=request.created_at,
        )
        priority_strata = _v2_priority_strata(
            calibration_repository=research_calibration_repository,
            cross_market_repository=cross_market_research_repository,
            source_calibration_id=request.source_calibration_id,
            source_agenda_id=plan.source_agenda_id,
        )
        walk_forward_subset = frozenset(
            select_walk_forward_null_subset(specs=specs, priority_strata=priority_strata)
        )
        if start:
            RegisterCalibrationRunV2(
                calibration_repository=research_calibration_repository
            ).execute(calibration=plan, specs=specs, created_at=request.created_at)
        run = AdvanceCalibrationRunV2(
            calibration_repository=research_calibration_repository,
            calibration_v2_repository=research_calibration_v2_repository,
            methodology_repository=research_methodology_repository,
            timeline=timeline,
            walk_forward_spec_ids=walk_forward_subset,
        ).execute(
            calibration=plan,
            panel=panel,
            maximum_specs=request.maximum_specs,
            created_at=request.created_at,
        )
        finalized_report = None
        if run.status.value == "COMPLETE":
            finalized_report = (
                FinalizeCalibrationV2(
                    calibration_repository=research_calibration_repository,
                    calibration_v2_repository=research_calibration_v2_repository,
                    methodology_repository=research_methodology_repository,
                    timeline=timeline,
                )
                .execute(calibration=plan, run=run, panel=panel, created_at=request.created_at)
                .report
            )
        return _research_calibration_envelope(
            {
                "calibration_run": run,
                "panel_warnings": panel.warnings,
                "walk_forward_priority_strata": tuple(sorted(priority_strata)),
                "walk_forward_subset_size": len(walk_forward_subset),
                "report": finalized_report,
                "financial_authority": False,
            }
        )

    @app.post("/api/v1/securities/research-calibration-v2/plans/{calibration_id}/run/start")
    def research_calibration_v2_run_start(
        calibration_id: str, request: ResearchCalibrationV2RunStepRequest
    ) -> dict[str, object]:
        return _run_calibration_v2_step(calibration_id=calibration_id, request=request, start=True)

    @app.post("/api/v1/securities/research-calibration-v2/plans/{calibration_id}/run/resume")
    def research_calibration_v2_run_resume(
        calibration_id: str, request: ResearchCalibrationV2RunStepRequest
    ) -> dict[str, object]:
        return _run_calibration_v2_step(calibration_id=calibration_id, request=request, start=False)

    @app.get("/api/v1/securities/research-calibration-v2/plans/{calibration_id}/run")
    def research_calibration_v2_run_inspect(calibration_id: str) -> dict[str, object]:
        run = research_calibration_repository.run_by_calibration(calibration_id)
        if run is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_V2_RUN_NOT_FOUND")
        outcomes = research_calibration_v2_repository.outcomes_for_run(run.calibration_run_id)
        return _research_calibration_envelope(
            {
                "calibration_run": run,
                "outcomes_completed": len(outcomes),
                "report": research_calibration_repository.report_by_calibration(calibration_id),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-calibration-v2/plans/{calibration_id}/strata")
    def research_calibration_v2_strata_inspect(
        calibration_id: str, research_design: str | None = None
    ) -> dict[str, object]:
        from apex.domain.research_calibration_v2 import MINIMUM_MATCHED_NULL_COUNT

        specs = research_calibration_v2_repository.specs_for_calibration(calibration_id)
        if research_design:
            specs = tuple(item for item in specs if item.research_design.value == research_design)
        counts: dict[str, int] = {}
        for spec in specs:
            counts[spec.matched_stratum] = counts.get(spec.matched_stratum, 0) + 1
        strata = tuple(
            {
                "matched_stratum": stratum,
                "control_count": count,
                "sufficient": count >= MINIMUM_MATCHED_NULL_COUNT,
            }
            for stratum, count in sorted(counts.items())
        )
        return _research_calibration_envelope({"strata": strata, "financial_authority": False})

    @app.get("/api/v1/securities/research-calibration-v2/plans/{calibration_id}/stability")
    def research_calibration_v2_stability_inspect(calibration_id: str) -> dict[str, object]:
        slices = research_calibration_v2_repository.stability_slices_for_calibration(calibration_id)
        return _research_calibration_envelope(
            {
                "stability_slices": slices,
                "financial_authority": False,
            }
        )

    @app.get(
        "/api/v1/securities/research-calibration-v2/plans/{calibration_id}"
        "/subjects/{subject_id}/evidence-grade"
    )
    def research_calibration_v2_evidence_grade_inspect(
        calibration_id: str, subject_id: str
    ) -> dict[str, object]:
        classification = research_calibration_v2_repository.classification_for_subject(
            calibration_id=calibration_id, subject_id=subject_id
        )
        if classification is None:
            raise HTTPException(
                status_code=404, detail="EVIDENCE_GRADE_V2_CLASSIFICATION_NOT_FOUND"
            )
        return _research_calibration_envelope(
            {"classification": classification, "financial_authority": False}
        )

    class ResearchCalibrationV2RecalibrateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        source_calibration_id: str = Field(min_length=1, max_length=160)
        created_at: datetime

    @app.post(
        "/api/v1/securities/research-calibration-v2/plans/{calibration_id}/recalibrate-agenda"
    )
    def research_calibration_v2_recalibrate_agenda(
        calibration_id: str, request: ResearchCalibrationV2RecalibrateRequest
    ) -> dict[str, object]:
        from apex.application.research_calibration_v2 import (
            ReassessGenuineFindingsV2,
            build_matched_pools,
            collect_v1_matched_pool,
        )
        from apex.cli import _v2_outcome_family_value, _v2_target_hypothesis_ids
        from apex.domain.cross_market_research import OutcomeFamily
        from apex.domain.research_calibration_v2 import (
            ConditioningShape,
            ResearchDesign,
            matched_stratum_key,
            research_design_for_hypothesis,
        )

        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_V2_PLAN_NOT_FOUND")
        report = research_calibration_repository.report_by_calibration(calibration_id)
        if report is None:
            raise HTTPException(
                status_code=409, detail="CALIBRATION_V2_REPORT_NOT_FOUND_RUN_TO_COMPLETION_FIRST"
            )
        classifications = research_calibration_v2_repository.classifications_for_calibration(
            calibration_id
        )
        by_stratum, pooled = build_matched_pools(classifications)
        target_ids = _v2_target_hypothesis_ids(
            calibration_repository=research_calibration_repository,
            source_calibration_id=request.source_calibration_id,
        )
        hypotheses = tuple(
            item
            for item in cross_market_research_repository.hypotheses_for_agenda(
                plan.source_agenda_id
            )
            if item.hypothesis_id in target_ids
        )
        v1_specs = research_calibration_repository.specs_for_calibration(
            request.source_calibration_id
        )
        v1_run = research_calibration_repository.run_by_calibration(request.source_calibration_id)
        v1_outcomes = (
            research_calibration_repository.outcomes_for_run(v1_run.calibration_run_id)
            if v1_run is not None
            else ()
        )
        merged_pools = dict(by_stratum)
        for hypothesis in hypotheses:
            design = research_design_for_hypothesis(
                template_id=hypothesis.template_id, market_scope=hypothesis.market_scope
            )
            if design is not ResearchDesign.PREDICTIVE:
                continue
            outcome_family = OutcomeFamily(_v2_outcome_family_value(hypothesis.outcome_id.value))
            stratum = matched_stratum_key(
                research_design=ResearchDesign.PREDICTIVE,
                outcome_family=outcome_family,
                horizon_sessions=hypothesis.horizon_sessions,
                market_scope=hypothesis.market_scope,
                conditioning_shape=ConditioningShape.UNCONDITIONAL,
            )
            v1_pool = collect_v1_matched_pool(
                v1_specs=v1_specs,
                v1_outcomes=v1_outcomes,
                methodology_repository=research_methodology_repository,
                outcome_family=outcome_family,
                horizon_sessions=hypothesis.horizon_sessions,
            )
            if v1_pool:
                merged_pools[stratum] = merged_pools.get(stratum, ()) + v1_pool
        results = ReassessGenuineFindingsV2(
            cross_market_repository=cross_market_research_repository,
            methodology_repository=research_methodology_repository,
        ).execute(
            calibration=plan,
            hypotheses=hypotheses,
            matched_pools=merged_pools,
            pooled=pooled,
            created_at=request.created_at,
        )
        for classification, recalibration in results:
            research_calibration_v2_repository.save_classification(classification)
            research_calibration_repository.save_recalibration(recalibration)
        return _research_calibration_envelope(
            {
                "hypotheses_reassessed": len(results),
                "classifications": tuple(item for item, _r in results),
                "recalibrations": tuple(item for _c, item in results),
                "financial_authority": False,
            }
        )

    class ResearchCalibrationV2RequalifyRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        created_at: datetime

    @app.post(
        "/api/v1/securities/research-calibration-v2/plans/{calibration_id}/follow-ups/requalify"
    )
    def research_calibration_v2_requalify_follow_ups(
        calibration_id: str, request: ResearchCalibrationV2RequalifyRequest
    ) -> dict[str, object]:
        from apex.application.research_calibration_v2 import RequalifyFollowUpsV2

        plan = research_calibration_repository.plan_by_id(calibration_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="CALIBRATION_V2_PLAN_NOT_FOUND")
        follow_ups = cross_market_research_repository.list_follow_ups()
        classifications = {
            item.subject_id: item
            for item in research_calibration_v2_repository.classifications_for_calibration(
                calibration_id
            )
            if item.subject_kind == "HYPOTHESIS"
        }
        hypotheses_by_id = {
            item.hypothesis_id: item
            for item in cross_market_research_repository.hypotheses_for_agenda(
                plan.source_agenda_id
            )
        }
        results = RequalifyFollowUpsV2(
            cross_market_repository=cross_market_research_repository
        ).execute(
            calibration=plan,
            follow_up_ids=[item.follow_up_id for item in follow_ups],
            hypotheses_by_id=hypotheses_by_id,
            hypothesis_classifications=classifications,
            created_at=request.created_at,
        )
        for item in results:
            research_calibration_v2_repository.save_follow_up_qualification(item)
        return _research_calibration_envelope(
            {"follow_up_qualifications": results, "financial_authority": False}
        )

    @app.get(
        "/api/v1/securities/research-calibration-v2/plans/{calibration_id}/follow-ups/qualifications"
    )
    def research_calibration_v2_follow_up_qualifications(calibration_id: str) -> dict[str, object]:
        return _research_calibration_envelope(
            {
                "items": (
                    research_calibration_v2_repository.follow_up_qualifications_for_calibration(
                        calibration_id
                    )
                ),
                "financial_authority": False,
            }
        )

    # -- Calibration-Qualified Interaction Replication v1 --------------------
    # Extends the existing research-calibration-v2 surface above rather than
    # creating a new dashboard: same "/api/v1/securities/..." namespace, same
    # envelope helper, same authority-NONE convention. Scoped to exactly the
    # one CALIBRATION_QUALIFIED follow-up this milestone runs; never creates
    # a Strategy, Shared Opportunity, PAPER order, or LIVE authority.

    def _research_replication_envelope(value: object) -> dict[str, object]:
        return _securities_envelope(value)

    class ResearchReplicationCreateRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        follow_up_id: str = Field(min_length=1, max_length=160)
        v2_calibration_id: str = Field(min_length=1, max_length=160)
        created_at: datetime

    @app.post("/api/v1/securities/research-replication/specifications")
    def research_replication_create(request: ResearchReplicationCreateRequest) -> dict[str, object]:
        from apex.application.research_replication import (
            FreezeReplicationSpecification,
            ReplicationApplicationError,
            ReplicationNotFound,
        )

        try:
            specification = FreezeReplicationSpecification(
                cross_market_repository=cross_market_research_repository,
                methodology_repository=research_methodology_repository,
                calibration_v1_repository=research_calibration_repository,
                calibration_v2_repository=research_calibration_v2_repository,
                replication_repository=research_replication_repository,
            ).execute(
                follow_up_id=request.follow_up_id,
                v2_calibration_id=request.v2_calibration_id,
                created_at=request.created_at,
            )
        except ReplicationNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ReplicationApplicationError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _research_replication_envelope(
            {"specification": specification, "financial_authority": False}
        )

    @app.get("/api/v1/securities/research-replication/specifications/{replication_id}")
    def research_replication_inspect(replication_id: str) -> dict[str, object]:
        specification = research_replication_repository.specification_by_id(replication_id)
        if specification is None:
            raise HTTPException(status_code=404, detail="REPLICATION_SPECIFICATION_NOT_FOUND")
        return _research_replication_envelope(
            {
                "specification": specification,
                "partitions": research_replication_repository.partitions_for_replication(
                    replication_id
                ),
                "conclusion": research_replication_repository.conclusion_for_replication(
                    replication_id
                ),
                "financial_authority": False,
            }
        )

    class ResearchReplicationRunRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

        created_at: datetime

    @app.post("/api/v1/securities/research-replication/specifications/{replication_id}/run")
    def research_replication_run(
        replication_id: str, request: ResearchReplicationRunRequest
    ) -> dict[str, object]:
        from apex.application.research_replication import RunReplication

        specification = research_replication_repository.specification_by_id(replication_id)
        if specification is None:
            raise HTTPException(status_code=404, detail="REPLICATION_SPECIFICATION_NOT_FOUND")
        result = RunReplication(
            historical_repository=historical_market_data_repository,
            evidence=securities_repository,
            genuine_repository=genuine_multi_asset_repository,
            crypto_repository=crypto_research_evidence_repository,
            replication_repository=research_replication_repository,
        ).execute(specification=specification, created_at=request.created_at)
        return _research_replication_envelope(
            {
                "timing": result.timing,
                "partitions": result.partitions,
                "effect_profile": result.effect_profile,
                "confound": result.confound,
                "conclusion": result.conclusion,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-replication/specifications/{replication_id}/stability")
    def research_replication_stability(replication_id: str) -> dict[str, object]:
        partitions = research_replication_repository.partitions_for_replication(replication_id)
        if not partitions:
            raise HTTPException(status_code=404, detail="REPLICATION_PARTITIONS_NOT_FOUND")
        return _research_replication_envelope(
            {
                "temporal": tuple(
                    item for item in partitions if item.partition_kind.value == "TIME_PERIOD"
                ),
                "volatility_state": tuple(
                    item for item in partitions if item.partition_kind.value == "VOLATILITY_STATE"
                ),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/research-replication/specifications/{replication_id}/timing")
    def research_replication_timing(replication_id: str) -> dict[str, object]:
        timing = research_replication_repository.timings_for_replication(replication_id)
        if not timing:
            raise HTTPException(status_code=404, detail="REPLICATION_TIMING_NOT_FOUND")
        return _research_replication_envelope({"timing": timing, "financial_authority": False})

    @app.get("/api/v1/securities/research-replication/specifications/{replication_id}/conclusion")
    def research_replication_conclusion(replication_id: str) -> dict[str, object]:
        conclusion = research_replication_repository.conclusion_for_replication(replication_id)
        if conclusion is None:
            raise HTTPException(status_code=404, detail="REPLICATION_CONCLUSION_NOT_FOUND")
        return _research_replication_envelope(
            {
                "conclusion": conclusion,
                "effect_profile": research_replication_repository.effect_profile_for_replication(
                    replication_id
                ),
                "confound": research_replication_repository.confound_assessment_for_replication(
                    replication_id
                ),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/genuine-multi-asset/registry")
    def genuine_multi_asset_registry(
        hypothesis_version_id: str | None = None, hypothesis_id: str | None = None
    ) -> dict[str, object]:
        """Milestone section 14/15: the one bounded, read-only, addressable
        summary of the genuine multi-asset cohort/capture/quality-gate/
        cross-market/replay/methodology lineage -- every id resolved live
        from retained evidence, authority always NONE."""

        from apex.adapters.genuine_multi_asset_history_postgres import (
            PostgresGenuineMultiAssetHistoryRepository,
        )
        from apex.application.genuine_multi_asset_history import (
            resolve_genuine_multi_asset_registry,
        )
        from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

        registry = resolve_genuine_multi_asset_registry(
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            historical_repository=historical_market_data_repository,
            genuine_repository=PostgresGenuineMultiAssetHistoryRepository(engine),
            replay_repository=historical_replay_repository,
            methodology_repository=research_methodology_repository,
            hypothesis_version_id=hypothesis_version_id,
            hypothesis_id=hypothesis_id,
        )
        return _securities_envelope(
            {
                "registry": registry,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/genuine-strategy-datasets")
    def securities_genuine_strategy_datasets() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                "items": securities_direction_dataset_repository.datasets(),
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/genuine-strategy-datasets/{dataset_id}")
    def securities_genuine_strategy_dataset(
        dataset_id: str,
        request: Request,
    ) -> dict[str, object]:
        dataset = securities_direction_dataset_repository.dataset(dataset_id)
        if dataset is None:
            raise _http_error(
                request,
                404,
                "SECURITIES_GENUINE_DATASET_NOT_FOUND",
                "genuine Securities Strategy Lab dataset was not found",
            )
        return _securities_envelope(
            {
                "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                "dataset": dataset,
                "financial_authority": False,
            }
        )

    @app.get("/api/v1/securities/instruments/{instrument_id}")
    def securities_instrument_detail(instrument_id: str, request: Request) -> dict[str, object]:
        try:
            return _securities_envelope(
                {
                    "warning": SECURITIES_SHADOW_WARNING,
                    "instrument": securities_repository.get_instrument(instrument_id),
                }
            )
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 404, "SECURITIES_INSTRUMENT_NOT_FOUND", str(error)
            ) from error

    @app.get("/api/v1/securities/data-batches")
    def securities_data_batches() -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": SECURITIES_SHADOW_WARNING,
                "items": securities_repository.list_batches(),
            }
        )

    @app.post(
        "/api/v1/securities/data-batches",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_securities_data_batch(
        body: SecuritiesDataBatchRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            batch = ImportSecuritiesData(
                repository=securities_repository, market_data=securities_data
            ).execute(
                ImportSecuritiesDataCommand(
                    managed_source=body.managed_source,
                    dataset_name=body.dataset_name,
                    idempotency_key=idempotency_key,
                    imported_at=datetime.now(tz=UTC),
                )
            )
            return _securities_envelope({"warning": SECURITIES_SHADOW_WARNING, "batch": batch})
        except SecuritiesIdempotencyConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (SecuritiesApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "SECURITIES_DATA_IMPORT_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/securities/data-batches/{batch_id}")
    def securities_data_batch_detail(batch_id: str, request: Request) -> dict[str, object]:
        try:
            return _securities_envelope(
                {
                    "warning": SECURITIES_SHADOW_WARNING,
                    "batch": securities_repository.get_batch(batch_id),
                }
            )
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 404, "SECURITIES_DATA_BATCH_NOT_FOUND", str(error)
            ) from error

    @app.get("/api/v1/securities/data-batches/{batch_id}/corporate-actions")
    def securities_corporate_actions(batch_id: str, request: Request) -> dict[str, object]:
        try:
            _instruments, _sessions, _bars, actions = securities_repository.get_dataset(batch_id)
            return _securities_envelope({"warning": SECURITIES_SHADOW_WARNING, "items": actions})
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 404, "SECURITIES_DATA_BATCH_NOT_FOUND", str(error)
            ) from error

    @app.get("/api/v1/securities/specifications")
    def securities_specifications() -> dict[str, object]:
        ensure_securities_baselines()
        return _securities_envelope(
            {
                "warning": SECURITIES_SHADOW_WARNING,
                "items": securities_repository.list_specifications(),
            }
        )

    def run_securities_research_request(
        *,
        body: SecuritiesResearchRequest,
        request: Request,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            ensure_securities_baselines()
            run = RunSecuritiesResearch(
                repository=securities_repository,
                campaigns=securities_campaigns,
                market_data=securities_data,
            ).execute(
                RunSecuritiesResearchCommand(
                    campaign_id=body.campaign_id,
                    strategy_id=body.strategy_id,
                    data_batch_id=body.data_batch_id,
                    universe=tuple(body.universe),
                    start_date=body.start,
                    end_date=body.end,
                    decision_cutoff=body.decision_cutoff,
                    benchmark_instrument_id=body.benchmark_instrument_id,
                    idempotency_key=idempotency_key,
                )
            )
            return _securities_envelope({"warning": SECURITIES_SHADOW_WARNING, "research_run": run})
        except SecuritiesIdempotencyConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (SecuritiesApplicationError, ValueError) as error:
            raise _http_error(request, 422, "SECURITIES_RESEARCH_REJECTED", str(error)) from error

    @app.get("/api/v1/securities/research-runs")
    def securities_research_runs(campaign_id: str | None = None) -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": SECURITIES_SHADOW_WARNING,
                "items": securities_repository.list_research_runs(campaign_id=campaign_id),
            }
        )

    @app.post(
        "/api/v1/securities/research-runs",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_securities_research_run(
        body: SecuritiesResearchRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return run_securities_research_request(
            body=body, request=request, idempotency_key=idempotency_key
        )

    @app.post(
        "/api/v1/securities/strategy-lab-research-runs",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_securities_strategy_lab_research_run(
        body: SecuritiesStrategyLabResearchRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        deployment = strategy_lab_shadow_store.deployment(body.deployment_id)
        if deployment is None:
            raise _http_error(
                request, 404, "SHADOW_DEPLOYMENT_NOT_FOUND", "deployment was not found"
            )
        try:
            specification = SecuritiesStrategySpecification.create(
                strategy_id=f"securities-strategy-lab-{deployment.deployment_id}",
                kind=SecuritiesStrategyKind.STRATEGY_LAB_MODEL,
                version="1",
                lookback_sessions=20,
                rebalance_every_sessions=1,
                strategy_lab_deployment_id=deployment.deployment_id,
                created_at=datetime.now(tz=UTC),
            )
            securities_repository.save_specification(specification)
            run = RunSecuritiesResearch(
                repository=securities_repository,
                campaigns=securities_campaigns,
                market_data=securities_data,
                model_signal_builder=StrategyLabSecuritiesSignalBuilder(
                    deployment=deployment,
                    artifact_store=strategy_lab_artifacts,
                    model_loader=StrategyLabManagedModelLoader(),
                ),
            ).execute(
                RunSecuritiesResearchCommand(
                    campaign_id=body.campaign_id,
                    strategy_id=specification.strategy_id,
                    data_batch_id=body.data_batch_id,
                    universe=tuple(body.universe),
                    start_date=body.start,
                    end_date=body.end,
                    decision_cutoff=body.decision_cutoff,
                    benchmark_instrument_id=body.benchmark_instrument_id,
                    idempotency_key=idempotency_key,
                )
            )
            return _securities_envelope({"warning": SECURITIES_SHADOW_WARNING, "research_run": run})
        except SecuritiesIdempotencyConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (SecuritiesApplicationError, ValueError) as error:
            raise _http_error(request, 422, "SECURITIES_RESEARCH_REJECTED", str(error)) from error

    @app.get("/api/v1/securities/research-runs/{research_run_id}")
    def securities_research_run_detail(research_run_id: str, request: Request) -> dict[str, object]:
        try:
            return _securities_envelope(
                {
                    "warning": SECURITIES_SHADOW_WARNING,
                    "research_run": securities_repository.get_research_run(research_run_id),
                }
            )
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 404, "SECURITIES_RESEARCH_RUN_NOT_FOUND", str(error)
            ) from error

    @app.get("/api/v1/securities/research-runs/{research_run_id}/signals")
    def securities_research_signals(research_run_id: str, request: Request) -> dict[str, object]:
        try:
            securities_repository.get_research_run(research_run_id)
            return _securities_envelope(
                {
                    "warning": SECURITIES_SHADOW_WARNING,
                    "items": securities_repository.list_signals(research_run_id),
                }
            )
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 404, "SECURITIES_RESEARCH_RUN_NOT_FOUND", str(error)
            ) from error

    @app.get("/api/v1/securities/conformance")
    def securities_conformance_reports(
        research_run_id: str | None = None,
    ) -> dict[str, object]:
        return _securities_envelope(
            {
                "warning": "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY",
                "items": securities_repository.list_conformance_reports(
                    research_run_id=research_run_id
                ),
            }
        )

    @app.post(
        "/api/v1/securities/conformance",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_securities_conformance_report(
        body: SecuritiesConformanceRequest,
        request: Request,
        _idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            report = RunSecuritiesConformance(
                repository=securities_repository,
                engines=(lean_securities_engine,),
            ).execute(
                RunSecuritiesConformanceCommand(
                    research_run_id=body.research_run_id,
                    external_engine=body.external_engine,
                    created_at=datetime.now(tz=UTC),
                )
            )
            return _securities_envelope(
                {
                    "warning": "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY",
                    "report": report,
                }
            )
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 422, "SECURITIES_CONFORMANCE_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/securities/conformance/{report_id}")
    def securities_conformance_report_detail(report_id: str, request: Request) -> dict[str, object]:
        try:
            return _securities_envelope(
                {
                    "warning": "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY",
                    "report": securities_repository.get_conformance_report(report_id),
                }
            )
        except SecuritiesApplicationError as error:
            raise _http_error(
                request, 404, "SECURITIES_CONFORMANCE_NOT_FOUND", str(error)
            ) from error

    def _baseline_payload(value: dict[str, object]) -> dict[str, object]:
        return {
            "warnings": (
                CANONICAL_BASELINE_WARNING,
                BASELINE_RESEARCH_WARNING,
                EXTERNAL_SIMULATOR_WARNING,
            ),
            **value,
        }

    @app.get("/api/v1/securities/baselines")
    def canonical_baseline_catalog() -> dict[str, object]:
        return _securities_envelope(
            _baseline_payload(
                {"items": canonical_securities_baselines(datetime(2026, 1, 1, tzinfo=UTC))}
            )
        )

    @app.get("/api/v1/securities/baseline-runs")
    def canonical_baseline_runs() -> dict[str, object]:
        return _securities_envelope(
            _baseline_payload({"items": securities_baseline_repository.runs()})
        )

    @app.get("/api/v1/securities/baseline-scorecards")
    def canonical_baseline_scorecards(
        data_batch_id: str | None = None,
    ) -> dict[str, object]:
        scorecards = securities_baseline_repository.scorecards()
        if data_batch_id is not None:
            scorecards = tuple(item for item in scorecards if item.data_batch_id == data_batch_id)
        return _securities_envelope(_baseline_payload({"items": scorecards}))

    @app.get("/api/v1/securities/simulator-conformance")
    def canonical_simulator_conformance() -> dict[str, object]:
        return _securities_envelope(
            _baseline_payload({"items": securities_baseline_repository.reports()})
        )

    @app.get("/api/v1/securities/simulator-conformance/current")
    def current_canonical_simulator_conformance() -> dict[str, object]:
        """Expose retained-request replacements alongside immutable history."""
        return _securities_envelope(
            _baseline_payload(
                {
                    "items": select_current_securities_conformance_reports(
                        reports=securities_baseline_repository.reports(),
                        runs=securities_baseline_repository.runs(),
                    ),
                    "frozen_shadow_blockers": (
                        "PBO_ABOVE_MAXIMUM",
                        "SIMULATOR_MISMATCH",
                    ),
                }
            )
        )

    @app.get("/api/v1/securities/sustained-shadow-operations")
    def sustained_shadow_operations() -> dict[str, object]:
        """Read-only operational state for the observation-only Securities plans."""
        from apex.application.sustained_shadow import SustainedShadowService

        service = SustainedShadowService(sustained_shadow_repository)
        plans = sustained_shadow_repository.plans()
        runtime_path = (
            Path(__file__).resolve().parents[2] / ".local" / "runtime" / "shadow-runtime.json"
        )
        try:
            runtime = json.loads(runtime_path.read_text()) if runtime_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            runtime = {}
        return _securities_envelope(
            _baseline_payload(
                {
                    "items": tuple(
                        {
                            "plan": plan,
                            "report": service.report(plan),
                            "parent_plan_id": plan.parent_plan_id,
                            "operation_jobs": (
                                sustained_shadow_operations_repository.jobs(policy.policy_id)
                                if (
                                    policy := sustained_shadow_operations_repository.policy(
                                        plan.plan_id
                                    )
                                )
                                else ()
                            ),
                        }
                        for plan in plans
                    ),
                    "runtime": runtime if isinstance(runtime, dict) else {},
                    "warnings": (
                        "OBSERVATION_ONLY",
                        "NO PAPER, BROKER, OR LIVE AUTHORITY",
                    ),
                }
            )
        )

    def _sustained_shadow_plan_or_404(plan_id: str, request: Request) -> Any:
        plan = sustained_shadow_repository.plan(plan_id)
        if plan is None:
            raise _http_error(
                request,
                404,
                "SUSTAINED_SHADOW_PLAN_NOT_FOUND",
                "Sustained SHADOW plan was not found.",
            )
        return plan

    @app.post("/api/v1/securities/sustained-shadow-plans/{plan_id}/activate")
    def activate_sustained_shadow_plan(
        plan_id: str, body: SustainedShadowConfirmationRequest, request: Request
    ) -> dict[str, object]:
        from apex.application.sustained_shadow import SustainedShadowError, SustainedShadowService

        try:
            plan = _sustained_shadow_plan_or_404(plan_id, request)
            return _securities_envelope(
                {
                    "plan": SustainedShadowService(sustained_shadow_repository).activate(
                        plan, body.confirm_plan_id
                    ),
                    "authority": "OBSERVATION_ONLY",
                }
            )
        except (SustainedShadowError, ValueError) as error:
            raise _http_error(
                request, 422, "SUSTAINED_SHADOW_ACTIVATION_REJECTED", str(error)
            ) from error

    @app.post("/api/v1/securities/sustained-shadow-plans/{plan_id}/pause")
    def pause_sustained_shadow_plan(
        plan_id: str, body: SustainedShadowPauseRequest, request: Request
    ) -> dict[str, object]:
        from apex.application.sustained_shadow import SustainedShadowError, SustainedShadowService

        try:
            plan = _sustained_shadow_plan_or_404(plan_id, request)
            return _securities_envelope(
                {
                    "plan": SustainedShadowService(sustained_shadow_repository).pause(
                        plan, body.operator_reason
                    ),
                    "authority": "OBSERVATION_ONLY",
                }
            )
        except (SustainedShadowError, ValueError) as error:
            raise _http_error(
                request, 422, "SUSTAINED_SHADOW_PAUSE_REJECTED", str(error)
            ) from error

    @app.post("/api/v1/securities/sustained-shadow-plans/{plan_id}/resume")
    def resume_sustained_shadow_plan(
        plan_id: str, body: SustainedShadowConfirmationRequest, request: Request
    ) -> dict[str, object]:
        from apex.application.sustained_shadow import SustainedShadowError, SustainedShadowService

        try:
            plan = _sustained_shadow_plan_or_404(plan_id, request)
            return _securities_envelope(
                {
                    "plan": SustainedShadowService(sustained_shadow_repository).resume(
                        plan, body.confirm_plan_id
                    ),
                    "authority": "OBSERVATION_ONLY",
                }
            )
        except (SustainedShadowError, ValueError) as error:
            raise _http_error(
                request, 422, "SUSTAINED_SHADOW_RESUME_REJECTED", str(error)
            ) from error

    @app.post("/api/v1/securities/sustained-shadow-plans/{plan_id}/revoke")
    def revoke_sustained_shadow_plan(
        plan_id: str, body: SustainedShadowConfirmationRequest, request: Request
    ) -> dict[str, object]:
        from apex.application.sustained_shadow import SustainedShadowError, SustainedShadowService

        try:
            plan = _sustained_shadow_plan_or_404(plan_id, request)
            return _securities_envelope(
                {
                    "plan": SustainedShadowService(sustained_shadow_repository).revoke(
                        plan, body.confirm_plan_id
                    ),
                    "authority": "OBSERVATION_ONLY",
                }
            )
        except (SustainedShadowError, ValueError) as error:
            raise _http_error(
                request, 422, "SUSTAINED_SHADOW_REVOKE_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/portfolio-intelligence")
    def portfolio_intelligence_runs() -> dict[str, object]:
        """Read-only research portfolio runs; no allocation is executable."""
        from apex.application.portfolio_intelligence import command_center

        return _securities_envelope(
            _baseline_payload(
                {
                    "items": tuple(
                        {
                            "run": run,
                            "command_center": command_center(
                                portfolio=run,
                                shadow={"authority": "OBSERVATION_ONLY"},
                            ),
                        }
                        for run in portfolio_intelligence_repository.runs()
                    ),
                    "warning": (
                        "HISTORICAL/FORWARD-EARLY RESEARCH CAPITAL PLAN — "
                        "NOT A TRADING RECOMMENDATION"
                    ),
                    "financial_authority": "NONE",
                }
            )
        )

    @app.get("/api/v1/research/hypotheses/{hypothesis_version_id}")
    def retained_research_hypothesis(
        hypothesis_version_id: str, request: Request
    ) -> dict[str, object]:
        value = research_evidence_repository.hypothesis_version(hypothesis_version_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_HYPOTHESIS_NOT_FOUND",
                "the exact retained hypothesis version was not found",
                entity_type="ResearchHypothesisVersion",
                entity_id=hypothesis_version_id,
            )
        return _securities_envelope(
            {"hypothesis": value, "authority": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/research/captures/{capture_id}")
    def retained_research_capture(capture_id: str, request: Request) -> dict[str, object]:
        value = research_evidence_repository.capture(capture_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_CAPTURE_NOT_FOUND",
                "the exact retained observation capture was not found",
                entity_type="ObservationCapture",
                entity_id=capture_id,
            )
        return _securities_envelope(
            {
                "capture": value,
                "checkpoints": research_evidence_repository.checkpoints(capture_id),
                "observations": research_evidence_repository.observations(capture_id),
                "raw_evidence_retained": (
                    research_evidence_repository.raw_payload(capture_id) is not None
                ),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/research/snapshots/{snapshot_id}")
    def retained_research_snapshot(snapshot_id: str, request: Request) -> dict[str, object]:
        value = research_evidence_repository.snapshot(snapshot_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_SNAPSHOT_NOT_FOUND",
                "the exact retained research dataset snapshot was not found",
                entity_type="ResearchDatasetSnapshot",
                entity_id=snapshot_id,
            )
        return _securities_envelope(
            {"snapshot": value, "authority": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/research/runs/{run_id}")
    def retained_research_run(run_id: str, request: Request) -> dict[str, object]:
        value = research_evidence_repository.run(run_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_RUN_NOT_FOUND",
                "the exact retained experiment run was not found",
                entity_type="ResearchExperimentRun",
                entity_id=run_id,
            )
        return _securities_envelope(
            {
                "run": value,
                "outcome": research_evidence_repository.outcome_for_run(run_id),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/research/outcomes/{outcome_id}")
    def retained_research_outcome(outcome_id: str, request: Request) -> dict[str, object]:
        value = research_evidence_repository.outcome(outcome_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_OUTCOME_NOT_FOUND",
                "the exact retained experiment outcome was not found",
                entity_type="ResearchExperimentOutcome",
                entity_id=outcome_id,
            )
        return _securities_envelope(
            {"outcome": value, "authority": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/crypto-research/hypotheses/{hypothesis_version_id}")
    def retained_crypto_hypothesis(
        hypothesis_version_id: str, request: Request
    ) -> dict[str, object]:
        hypothesis = crypto_research_repository.hypothesis_version(hypothesis_version_id)
        specification = crypto_research_repository.hypothesis_specification(hypothesis_version_id)
        if hypothesis is None or specification is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_HYPOTHESIS_NOT_FOUND",
                "the exact retained crypto hypothesis version was not found",
                entity_type="CryptoHypothesisSpecification",
                entity_id=hypothesis_version_id,
            )
        return _securities_envelope(
            {
                "hypothesis": hypothesis,
                "specification": specification,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/captures/{capture_id}")
    def retained_crypto_capture(capture_id: str, request: Request) -> dict[str, object]:
        from apex.application.crypto_campaign import campaign_workspace

        capture = crypto_research_repository.capture(capture_id)
        if capture is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CAPTURE_NOT_FOUND",
                "the exact retained crypto capture was not found",
                entity_type="CryptoHistoricalCapture",
                entity_id=capture_id,
            )
        campaign_view = campaign_workspace(crypto_research_repository)
        holdout = campaign_view.get("holdout")
        sealed_holdout = getattr(holdout, "capture_id", None) == capture_id and getattr(
            getattr(holdout, "sealing_state", None), "value", None
        ) in {"SEALED", "RELEASE_AUTHORIZED", "EVALUATED"}
        attempts = crypto_research_repository.attempts(capture_id)
        return _securities_envelope(
            {
                "capture": holdout if sealed_holdout else capture,
                "attempts": () if sealed_holdout else attempts,
                "checkpoints": crypto_research_repository.checkpoints(capture_id),
                "candle_count": capture.accepted_buckets,
                "raw_responses_retained": "REDACTED" if sealed_holdout else len(attempts),
                "values_redacted": sealed_holdout,
                "raw_responses_redacted": sealed_holdout,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/snapshots/{snapshot_id}")
    def retained_crypto_snapshot(snapshot_id: str, request: Request) -> dict[str, object]:
        from apex.application.crypto_campaign import campaign_workspace

        snapshot = crypto_research_repository.snapshot(snapshot_id)
        if snapshot is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_SNAPSHOT_NOT_FOUND",
                "the exact retained crypto historical snapshot was not found",
                entity_type="CryptoHistoricalSnapshot",
                entity_id=snapshot_id,
            )
        workspace = campaign_workspace(crypto_research_repository)
        holdout = workspace.get("holdout")
        if getattr(holdout, "snapshot_id", None) == snapshot_id:
            return _securities_envelope(
                {
                    "snapshot": holdout,
                    "values_redacted": True,
                    "raw_responses_redacted": True,
                    "authority": "NONE",
                    "financial_authority": "NONE",
                }
            )
        return _securities_envelope(
            {"snapshot": snapshot, "authority": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/crypto-research/exposures/{snapshot_id}")
    def retained_crypto_exposures(snapshot_id: str) -> dict[str, object]:
        from apex.domain.crypto_campaign import DatasetExposureSummary

        records = crypto_research_repository.exposures(snapshot_id)
        return _securities_envelope(
            {
                "summary": DatasetExposureSummary.from_records(snapshot_id, records),
                "records": records,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/campaigns/{campaign_id}")
    def retained_crypto_campaign(campaign_id: str, request: Request) -> dict[str, object]:
        from apex.application.crypto_campaign import campaign_workspace

        campaign = crypto_research_repository.campaign(campaign_id)
        if campaign is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CAMPAIGN_NOT_FOUND",
                "the exact retained crypto research campaign was not found",
                entity_type="CryptoResearchCampaign",
                entity_id=campaign_id,
            )
        return _securities_envelope(campaign_workspace(crypto_research_repository))

    @app.get("/api/v1/crypto-research/holdouts/{holdout_id}")
    def retained_crypto_holdout(holdout_id: str, request: Request) -> dict[str, object]:
        from apex.application.crypto_campaign import campaign_workspace

        view = campaign_workspace(crypto_research_repository)
        holdout = view.get("holdout")
        if getattr(holdout, "holdout_id", None) != holdout_id:
            raise _http_error(
                request,
                404,
                "CRYPTO_HOLDOUT_NOT_FOUND",
                "the exact retained crypto holdout was not found",
                entity_type="HoldoutMetadata",
                entity_id=holdout_id,
            )
        return _securities_envelope(
            {
                "holdout": holdout,
                "values_redacted": True,
                "raw_responses_redacted": True,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/runs/{run_id}")
    def retained_crypto_run(run_id: str, request: Request) -> dict[str, object]:
        run = crypto_research_repository.run(run_id)
        if run is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_RUN_NOT_FOUND",
                "the exact retained crypto experiment run was not found",
                entity_type="CryptoExperimentRun",
                entity_id=run_id,
            )
        return _securities_envelope(
            {
                "run": run,
                "outcome": crypto_research_repository.outcome_for_run(run_id),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/outcomes/{outcome_id}")
    def retained_crypto_outcome(outcome_id: str, request: Request) -> dict[str, object]:
        outcome = crypto_research_repository.outcome(outcome_id)
        if outcome is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_OUTCOME_NOT_FOUND",
                "the exact retained crypto experiment outcome was not found",
                entity_type="CryptoExperimentOutcome",
                entity_id=outcome_id,
            )
        return _securities_envelope(
            {
                "outcome": outcome,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/strategies/{specification_id}")
    def retained_crypto_strategy(specification_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.strategy_specification(specification_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_STRATEGY_NOT_FOUND",
                "the exact frozen crypto strategy specification was not found",
                entity_type="CryptoStrategySpecification",
                entity_id=specification_id,
            )
        return _securities_envelope({"strategy_specification": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/replication-protocols/{protocol_id}")
    def retained_crypto_replication_protocol(
        protocol_id: str, request: Request
    ) -> dict[str, object]:
        value = crypto_research_repository.replication_protocol(protocol_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_REPLICATION_PROTOCOL_NOT_FOUND",
                "the exact crypto replication protocol was not found",
                entity_type="CryptoReplicationProtocol",
                entity_id=protocol_id,
            )
        return _securities_envelope({"replication_protocol": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/replications/{run_id}")
    def retained_crypto_replication(run_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.replication_run(run_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_REPLICATION_NOT_FOUND",
                "the exact crypto disjoint replication run was not found",
                entity_type="CryptoReplicationRun",
                entity_id=run_id,
            )
        return _securities_envelope(
            {
                "replication": value,
                "outcome": crypto_research_repository.replication_outcome_for_run(run_id),
                "authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/replication-outcomes/{outcome_id}")
    def retained_crypto_replication_outcome(outcome_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.replication_outcome(outcome_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_REPLICATION_OUTCOME_NOT_FOUND",
                "the exact crypto replication outcome was not found",
                entity_type="CryptoReplicationOutcome",
                entity_id=outcome_id,
            )
        return _securities_envelope({"replication_outcome": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/dossiers/{dossier_id}")
    def retained_crypto_dossier(dossier_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.dossier(dossier_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_DOSSIER_NOT_FOUND",
                "the exact crypto strategy evidence dossier was not found",
                entity_type="CryptoEvidenceDossier",
                entity_id=dossier_id,
            )
        return _securities_envelope(
            {
                "dossier": value,
                "conclusion": crypto_research_repository.dossier_conclusion_for_dossier(dossier_id),
                "authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/dossier-conclusions/{conclusion_id}")
    def retained_crypto_dossier_conclusion(
        conclusion_id: str, request: Request
    ) -> dict[str, object]:
        value = crypto_research_repository.dossier_conclusion(conclusion_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_DOSSIER_CONCLUSION_NOT_FOUND",
                "the exact crypto dossier conclusion was not found",
                entity_type="CryptoDossierConclusion",
                entity_id=conclusion_id,
            )
        return _securities_envelope({"dossier_conclusion": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/negative-knowledge/{semantics_hash}")
    def retained_crypto_negative_knowledge(semantics_hash: str) -> dict[str, object]:
        value = crypto_research_repository.strategy_by_semantics(semantics_hash)
        return _securities_envelope(
            {
                "semantics_hash": semantics_hash,
                "status": "ALREADY_TESTED" if value is not None else "UNTESTED_SEMANTICS",
                "strategy_specification": value,
                "authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/challenger-hypotheses/{hypothesis_version_id}")
    def retained_crypto_challenger_hypothesis(
        hypothesis_version_id: str, request: Request
    ) -> dict[str, object]:
        hypothesis = crypto_research_repository.hypothesis_version(hypothesis_version_id)
        specification = crypto_research_repository.challenger_hypothesis(hypothesis_version_id)
        if hypothesis is None or specification is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_HYPOTHESIS_NOT_FOUND",
                "the exact retained challenger hypothesis was not found",
                entity_type="MonthlyMomentumHypothesisSpecification",
                entity_id=hypothesis_version_id,
            )
        return _securities_envelope(
            {
                "hypothesis": hypothesis,
                "specification": specification,
                "network_behavior": "RETAINED_EVIDENCE_ONLY_NO_PROVIDER_REQUEST",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/challenger-lineage/{specification_id}")
    def retained_crypto_challenger_lineage(
        specification_id: str, request: Request
    ) -> dict[str, object]:
        from apex.domain.crypto_challenger import StrategySemanticComparison

        strategy = crypto_research_repository.strategy_specification(specification_id)
        parent = (
            crypto_research_repository.strategy_specification(
                strategy.parent_specification_id or ""
            )
            if strategy is not None
            else None
        )
        if strategy is None or parent is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_LINEAGE_NOT_FOUND",
                "the exact retained challenger lineage was not found",
                entity_type="CryptoStrategySpecification",
                entity_id=specification_id,
            )
        return _securities_envelope(
            {
                "strategy": strategy,
                "parent": parent,
                "semantic_comparison": StrategySemanticComparison.create(
                    prior=parent, challenger=strategy
                ),
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/challenger-protocols/{protocol_id}")
    def retained_crypto_challenger_protocol(
        protocol_id: str, request: Request
    ) -> dict[str, object]:
        value = crypto_research_repository.challenger_protocol(protocol_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_PROTOCOL_NOT_FOUND",
                "the exact retained challenger protocol was not found",
                entity_type="CryptoChallengerProtocol",
                entity_id=protocol_id,
            )
        return _securities_envelope(
            {"protocol": value, "network_behavior": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/crypto-research/challenger-runs/{run_id}")
    def retained_crypto_challenger_run(run_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.challenger_run(run_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_RUN_NOT_FOUND",
                "the exact retained challenger run was not found",
                entity_type="CryptoChallengerRun",
                entity_id=run_id,
            )
        return _securities_envelope(
            {
                "run": value,
                "outcome": crypto_research_repository.challenger_outcome_for_run(run_id),
                "prior_strategy_comparison": value.prior_comparison,
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/challenger-outcomes/{outcome_id}")
    def retained_crypto_challenger_outcome(outcome_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.challenger_outcome(outcome_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_OUTCOME_NOT_FOUND",
                "the exact retained challenger outcome was not found",
                entity_type="CryptoChallengerOutcome",
                entity_id=outcome_id,
            )
        return _securities_envelope({"outcome": value, "financial_authority": "NONE"})

    @app.get("/api/v1/crypto-research/challenger-dossiers/{dossier_id}")
    def retained_crypto_challenger_dossier(dossier_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.challenger_dossier(dossier_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_DOSSIER_NOT_FOUND",
                "the exact retained challenger dossier was not found",
                entity_type="CryptoChallengerDossier",
                entity_id=dossier_id,
            )
        return _securities_envelope(
            {
                "dossier": value,
                "conclusion": (
                    crypto_research_repository.challenger_conclusion_for_dossier(dossier_id)
                ),
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/challenger-conclusions/{conclusion_id}")
    def retained_crypto_challenger_conclusion(
        conclusion_id: str, request: Request
    ) -> dict[str, object]:
        value = crypto_research_repository.challenger_conclusion(conclusion_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CHALLENGER_CONCLUSION_NOT_FOUND",
                "the exact retained challenger conclusion was not found",
                entity_type="CryptoChallengerConclusion",
                entity_id=conclusion_id,
            )
        return _securities_envelope(
            {
                "conclusion": value,
                "approved_champion_status": "NO_APPROVED_CHAMPION",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/candidate-definitions")
    def retained_crypto_candidate_definitions() -> dict[str, object]:
        from apex.application.crypto_campaign_generic import (
            retained_strategy_compatibility,
        )

        return _securities_envelope(
            {
                "items": crypto_research_repository.generic_definitions(),
                "compatibility_items": retained_strategy_compatibility(),
                "historical_records_mutated": False,
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/candidate-definitions/{definition_id}")
    def retained_crypto_candidate_definition(
        definition_id: str, request: Request
    ) -> dict[str, object]:
        value = crypto_research_repository.generic_definition(definition_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_GENERIC_CANDIDATE_DEFINITION_NOT_FOUND",
                "the exact generic candidate definition was not found",
                entity_type="GenericCandidateDefinition",
                entity_id=definition_id,
            )
        return _securities_envelope(
            {
                "definition": value,
                "compiled_plan": crypto_research_repository.generic_plan(
                    f"generic-crypto-plan-{value.semantics_hash[:24]}"
                ),
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/compiled-plans/{plan_id}")
    def retained_crypto_compiled_plan(plan_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.generic_plan(plan_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_GENERIC_COMPILED_PLAN_NOT_FOUND",
                "the exact generic compiled plan was not found",
                entity_type="CompiledCandidatePlan",
                entity_id=plan_id,
            )
        return _securities_envelope({"compiled_plan": value, "financial_authority": "NONE"})

    @app.get("/api/v1/crypto-research/campaign-candidates/{candidate_id}")
    def retained_crypto_campaign_candidate(
        candidate_id: str, request: Request
    ) -> dict[str, object]:
        value = crypto_research_repository.campaign_candidate(candidate_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CAMPAIGN_CANDIDATE_NOT_FOUND",
                "the exact retained campaign candidate was not found",
                entity_type="CampaignCandidate",
                entity_id=candidate_id,
            )
        return _securities_envelope(
            {
                "candidate": value,
                "material_differences": (
                    crypto_research_repository.retained_candidate_comparison(candidate_id)
                ),
                "paper_eligible": False,
                "live_eligible": False,
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/candidate-protocols/{protocol_id}")
    def retained_crypto_candidate_protocol(protocol_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.retained_candidate_protocol(protocol_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_NOT_FOUND",
                "the exact retained candidate protocol was not found",
                entity_type="CandidateProtocol",
                entity_id=protocol_id,
            )
        return _securities_envelope(
            {"protocol": value, "independent_validation": False, "financial_authority": "NONE"}
        )

    @app.get("/api/v1/crypto-research/candidate-runs/{run_id}")
    def retained_crypto_candidate_run(run_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.retained_candidate_run(run_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CAMPAIGN_CANDIDATE_RUN_NOT_FOUND",
                "the exact retained candidate run was not found",
                entity_type="CandidateRun",
                entity_id=run_id,
            )
        return _securities_envelope(
            {
                "run": value,
                "outcome": (crypto_research_repository.retained_candidate_outcome_for_run(run_id)),
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/candidate-dossiers/{dossier_id}")
    def retained_crypto_candidate_dossier(dossier_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.retained_candidate_dossier(dossier_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_NOT_FOUND",
                "the exact retained candidate dossier was not found",
                entity_type="CandidateDossier",
                entity_id=dossier_id,
            )
        return _securities_envelope(
            {
                "dossier": value,
                "nomination_evaluation": (
                    crypto_research_repository.retained_candidate_nomination_for_dossier(dossier_id)
                ),
                "holdout_values_redacted": True,
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/approved-champion")
    def retained_crypto_approved_champion() -> dict[str, object]:
        return _securities_envelope(
            {
                "status": "NO_APPROVED_CHAMPION",
                "capital_authority": "NONE",
                "opportunity_authority": "NONE",
                "order_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/workspace")
    def crypto_research_workspace_read() -> dict[str, object]:
        return _securities_envelope(
            _baseline_payload(_retained_crypto_research_workspace(crypto_research_repository))
        )

    @app.get("/api/v1/crypto-research/product-captures/{capture_id}")
    def crypto_product_capture_read(capture_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.product_capture(capture_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PRODUCT_CAPTURE_NOT_FOUND",
                "the exact retained public product capture was not found",
                entity_type="CryptoProductCapture",
                entity_id=capture_id,
            )
        return _securities_envelope(
            {
                "capture": value,
                "products": crypto_research_repository.products(capture_id),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/universe-policy")
    def crypto_universe_policy_read() -> dict[str, object]:
        from apex.application.crypto_universe import crypto_universe_workspace

        return _securities_envelope(
            {"policy": crypto_universe_workspace(crypto_research_repository)["policy"]}
        )

    @app.get("/api/v1/crypto-research/universes/{snapshot_id}")
    def crypto_universe_read(snapshot_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.universe_snapshot(snapshot_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_UNIVERSE_SNAPSHOT_NOT_FOUND",
                "the exact retained crypto universe snapshot was not found",
                entity_type="CryptoUniverseSnapshot",
                entity_id=snapshot_id,
            )
        return _securities_envelope(
            {"universe": value, "authority": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/crypto-research/observations/{observation_id}")
    def crypto_observation_read(observation_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.observation(observation_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PRODUCT_OBSERVATION_NOT_FOUND",
                "the exact retained crypto product observation was not found",
                entity_type="CryptoProductObservation",
                entity_id=observation_id,
            )
        return _securities_envelope(
            {
                "observation": value,
                "candle_count": len(value.candles),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/discoveries/{run_id}")
    def crypto_discovery_read(run_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.discovery_run(run_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_DISCOVERY_RUN_NOT_FOUND",
                "the exact retained crypto discovery run was not found",
                entity_type="CryptoDiscoveryRun",
                entity_id=run_id,
            )
        return _securities_envelope(
            {"discovery": value, "authority": "NONE", "financial_authority": "NONE"}
        )

    @app.get("/api/v1/crypto-research/opportunities/{opportunity_id}")
    def crypto_opportunity_read(opportunity_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.opportunity(opportunity_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_RESEARCH_OPPORTUNITY_NOT_FOUND",
                "the exact retained research-only crypto Opportunity was not found",
                entity_type="CryptoResearchOpportunity",
                entity_id=opportunity_id,
            )
        return _securities_envelope(
            {
                "opportunity": value,
                "not_an_order": True,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/pulse/plans/{plan_id}")
    def crypto_pulse_plan_read(plan_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.refresh_plan(plan_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PULSE_PLAN_NOT_FOUND",
                "the exact retained crypto market-pulse plan was not found",
                entity_type="CryptoRefreshPlan",
                entity_id=plan_id,
            )
        return _securities_envelope({"refresh_plan": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/pulse/refreshes/{refresh_id}")
    def crypto_pulse_refresh_read(refresh_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.refresh_run(refresh_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PULSE_REFRESH_NOT_FOUND",
                "the exact retained crypto market-pulse refresh was not found",
                entity_type="CryptoRefreshRun",
                entity_id=refresh_id,
            )
        return _securities_envelope(
            {
                "refresh": value,
                "events": crypto_research_repository.refresh_events(refresh_id),
                "checkpoints": crypto_research_repository.pulse_checkpoints(refresh_id),
                "authority": "NONE",
            }
        )

    @app.get("/api/v1/crypto-research/pulse/checkpoints/{checkpoint_id}")
    def crypto_pulse_checkpoint_read(checkpoint_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.pulse_checkpoint(checkpoint_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PULSE_CHECKPOINT_NOT_FOUND",
                "the exact retained crypto market-pulse checkpoint was not found",
                entity_type="CryptoPulseCheckpoint",
                entity_id=checkpoint_id,
            )
        return _securities_envelope({"checkpoint": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/pulse/snapshots/{pulse_id}")
    def crypto_pulse_snapshot_read(pulse_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.pulse_snapshot(pulse_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PULSE_SNAPSHOT_NOT_FOUND",
                "the exact retained crypto market-pulse snapshot was not found",
                entity_type="CryptoPulseSnapshot",
                entity_id=pulse_id,
            )
        return _securities_envelope({"pulse_snapshot": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/pulse/lineages/{lineage_id}")
    def crypto_pulse_lineage_read(lineage_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.opportunity_lineage(lineage_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PULSE_LINEAGE_NOT_FOUND",
                "the exact retained crypto Opportunity lineage was not found",
                entity_type="CryptoOpportunityLineage",
                entity_id=lineage_id,
            )
        return _securities_envelope({"opportunity_lineage": value, "authority": "NONE"})

    @app.get("/api/v1/crypto-research/pulse/summaries/{summary_id}")
    def crypto_pulse_summary_read(summary_id: str, request: Request) -> dict[str, object]:
        value = crypto_research_repository.market_change_summary(summary_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PULSE_SUMMARY_NOT_FOUND",
                "the exact retained crypto market-change summary was not found",
                entity_type="CryptoMarketChangeSummary",
                entity_id=summary_id,
            )
        return _securities_envelope({"market_change_summary": value, "authority": "NONE"})

    @app.get("/api/v1/shared-opportunities/workspace")
    def shared_opportunity_workspace() -> dict[str, object]:
        """Compact, read-only cross-venue research inbox."""
        workspace = shared_opportunity_repository.workspace()
        workspace["total_market_context"] = total_market_context_repository.workspace()
        workspace["operating_cycle"] = operating_cycle_repository.workspace()
        workspace["cross_venue_allocation"] = cross_venue_allocation_repository.workspace()
        workspace["performance_attribution"] = performance_attribution_repository.workspace()
        workspace["kalshi_operating"] = {
            "provider_capability": kalshi_operating_repository.latest_capability(),
            "latest_refresh": kalshi_operating_repository.latest_refresh(),
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
        }
        return _securities_envelope(workspace)

    def operator_console_sources() -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        values = (
            operating_cycle_repository.workspace(),
            shared_opportunity_repository.workspace(),
            cross_venue_allocation_repository.workspace(),
            performance_attribution_repository.workspace(),
        )
        return cast(
            tuple[
                dict[str, object],
                dict[str, object],
                dict[str, object],
                dict[str, object],
            ],
            tuple(_jsonable(value) for value in values),
        )

    def operator_console_paper() -> dict[str, object]:
        crypto_states = tuple(
            crypto_state
            for account in crypto_paper_repository.accounts()
            if (crypto_state := crypto_paper_repository.state(account.account_id)) is not None
        )
        kalshi_states = tuple(
            kalshi_state
            for account_id in kalshi_operating_repository.account_ids()
            if (kalshi_state := kalshi_operating_repository.state(account_id)) is not None
        )
        external = tuple(
            {
                "instruction": instruction,
                "submission_attempt": external_paper_repository.submission_attempt(
                    instruction.instruction_id
                ),
                "validated_fills": external_paper_repository.validated_fills_for_instruction(
                    instruction.instruction_id
                ),
                "reconciliations": external_paper_repository.reconciliations_for_instruction(
                    instruction.instruction_id
                ),
            }
            for instruction in external_paper_repository.instructions()
        )
        # PAPER Operations & Observability v1: real point-in-time (Live
        # Observation Point-in-Time Race Fix v1 -- as_of-bounded, never an
        # unbounded "latest" query) unrealized marks for every currently
        # OPEN crypto Position. Real Decimal arithmetic happens here, once,
        # before jsonification -- the read-model projector itself never
        # re-derives a financial total from already-stringified evidence.
        # Kalshi contracts have no equivalent point-in-time mark substrate
        # today (Live Market Observation Foundation v1 is CRYPTO/Coinbase-
        # only) and are never looked up here.
        mark_as_of = datetime.now(tz=UTC)
        position_marks: dict[str, object] = {}
        unrealized_totals: dict[str, object] = {}
        for crypto_state in crypto_states:
            account_total = Decimal(0)
            marked_any = False
            for position in crypto_state.positions:
                if position.state != "OPEN":
                    continue
                observation_result = ObtainContemporaneousMarketObservation(
                    live_observation_repository
                ).execute(
                    venue=SharedVenue.CRYPTO,
                    instrument_id=position.product_id,
                    observation_type=LiveObservationType.QUOTE,
                    as_of=mark_as_of,
                    max_staleness_seconds=1800,
                )
                if (
                    observation_result.disposition
                    is not ContemporaneousObservationDisposition.SATISFIED
                    or observation_result.observation is None
                ):
                    position_marks[position.position_id] = {
                        "disposition": observation_result.disposition.value,
                        "reasons": observation_result.reasons,
                    }
                    continue
                price = price_evidence_from_live_observation(
                    observation_result.observation, product_id=position.product_id
                ).price
                mark = estimate_open_position_mark(
                    position=position, mark_price=price, marked_at=mark_as_of
                )
                position_marks[position.position_id] = _jsonable(mark)
                account_total += mark.unrealized_paper_pnl
                marked_any = True
            if marked_any:
                unrealized_totals[crypto_state.account.account_id] = _jsonable(account_total)

        # PAPER Operations & Observability v1: the registered
        # STRATEGY_PAPER_TRIAL_EXECUTION strategy catalog crossed with the
        # most recent already-retained per-cycle evaluation -- see
        # build_operator_paper_strategy_registry's own docstring for why
        # this is never a second, independently-recomputed decision.
        strategy_account_state = crypto_paper_repository.state(
            GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID
        )
        open_trial_order_by_strategy_id: dict[str, object] = {}
        account_last_reconciled_at: object | None = None
        account_last_reconciled_at_raw: datetime | None = None
        if strategy_account_state is not None:
            open_trial_order_by_strategy_id = {
                strategy_id: _jsonable(order)
                for strategy_id, order in open_trial_orders_by_strategy(
                    positions=strategy_account_state.positions,
                    orders=strategy_account_state.orders,
                    authorizations=strategy_account_state.authorizations,
                ).items()
            }
            if strategy_account_state.reconciliations:
                account_last_reconciled_at_raw = max(
                    strategy_account_state.reconciliations,
                    key=lambda item: item.evaluated_at,
                ).evaluated_at
                account_last_reconciled_at = _jsonable(account_last_reconciled_at_raw)
        recent_cycles = sorted(
            operating_cycle_repository.cycles(), key=lambda item: item.created_at
        )[-15:]
        strategy_task_results = [
            row
            for cycle in recent_cycles
            for row in operating_cycle_repository.task_results(cycle.cycle_id)
            # OperatingTaskType is a StrEnum -- comparing its member against
            # the plain string is exact and avoids a new raw-domain-enum
            # import into a module that otherwise only ever compares
            # already-jsonified string values.
            if row.task_type == "STRATEGY_PAPER_TRIAL_EXECUTION"
        ]
        # This task type always reports awaits_intraday_continuation=True on
        # success (see adapters.operating_cycle_tasks
        # ._execute_strategy_paper_trial's own docstring), so
        # ExecuteOperatingCycle deliberately does not persist a terminal
        # OperatingTaskResult for it while it keeps succeeding -- only a
        # genuine exception forces one. "Most recent OperatingTaskResult" is
        # therefore a last-FAILURE signal, not a last-activity signal: a
        # task that failed once days ago and has succeeded silently every
        # cycle since shows that old failure here. account_last_reconciled_at
        # above (unconditionally refreshed every real dispatch, success or
        # not) is the honest "is it alive right now" cross-check threaded
        # through to the registry alongside it -- never presented alone.
        latest_strategy_task_result = (
            max(strategy_task_results, key=lambda row: row.completed_at)
            if strategy_task_results
            else None
        )
        # PAPER Continuation Health Signal v1: the one authoritative "is it
        # healthy right now" status, combining the terminal-result stream
        # above with account_last_reconciled_at_raw -- see
        # domain.operating_cycle.compute_paper_continuation_health's own
        # docstring for the full resolution rule. freshness_window reuses
        # the operating-cycle worker's own established tick cadence (the
        # doc's existing "within the last one or two cycle intervals"
        # heuristic, made precise) rather than inventing a new PAPER-only
        # constant. has_active_work is True exactly when some strategy
        # currently holds an open trial -- the same signal already used
        # for has_open_trial per row below.
        strategy_continuation_health = compute_paper_continuation_health(
            latest_terminal_status=(
                latest_strategy_task_result.status if latest_strategy_task_result else None
            ),
            latest_terminal_at=(
                latest_strategy_task_result.completed_at if latest_strategy_task_result else None
            ),
            latest_continuation_at=account_last_reconciled_at_raw,
            now=mark_as_of,
            freshness_window=timedelta(
                seconds=2 * SupervisedOperatingRunner.EVALUATION_CADENCE_SECONDS
            ),
            has_active_work=bool(open_trial_order_by_strategy_id),
        )

        # PAPER Continuation Health Signal v2 (cross-venue extension): the
        # identical signal above, extended to the older cross-venue
        # CRYPTO_PAPER_TRIAL_EXECUTION/KALSHI_PAPER_TRIAL_EXECUTION tasks.
        # Both report awaits_intraday_continuation=True on every successful
        # dispatch, exactly like _execute_strategy_paper_trial (see
        # adapters.operating_cycle_tasks._execute_crypto_paper_trial /
        # ._execute_kalshi_paper_trial's own docstrings), and both reconcile
        # unconditionally on every dispatch -- confirmed by reading both
        # executors, not assumed -- so the identical terminal-status/
        # continuation-evidence divergence this signal exists to resolve
        # applies to them too. Keyed by account_id (mirroring
        # unrealized_totals/position_marks above) rather than assuming a
        # single account per venue, even though exactly one exists today.
        open_order_states = {"SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_REQUESTED"}

        def _has_open_paper_activity(state: object) -> bool:
            positions = getattr(state, "positions", ())
            orders = getattr(state, "orders", ())
            return any(getattr(position, "state", None) == "OPEN" for position in positions) or any(
                getattr(order, "state", None) in open_order_states for order in orders
            )

        crypto_task_results = [
            row
            for cycle in recent_cycles
            for row in operating_cycle_repository.task_results(cycle.cycle_id)
            if row.task_type == "CRYPTO_PAPER_TRIAL_EXECUTION"
        ]
        latest_crypto_task_result = (
            max(crypto_task_results, key=lambda row: row.completed_at)
            if crypto_task_results
            else None
        )
        kalshi_task_results = [
            row
            for cycle in recent_cycles
            for row in operating_cycle_repository.task_results(cycle.cycle_id)
            if row.task_type == "KALSHI_PAPER_TRIAL_EXECUTION"
        ]
        latest_kalshi_task_result = (
            max(kalshi_task_results, key=lambda row: row.completed_at)
            if kalshi_task_results
            else None
        )
        cross_venue_continuation_health: dict[str, object] = {}
        for crypto_state in crypto_states:
            latest_crypto_reconciled_at = max(
                (item.evaluated_at for item in crypto_state.reconciliations), default=None
            )
            cross_venue_continuation_health[crypto_state.account.account_id] = _jsonable(
                compute_paper_continuation_health(
                    latest_terminal_status=(
                        latest_crypto_task_result.status if latest_crypto_task_result else None
                    ),
                    latest_terminal_at=(
                        latest_crypto_task_result.completed_at
                        if latest_crypto_task_result
                        else None
                    ),
                    latest_continuation_at=latest_crypto_reconciled_at,
                    now=mark_as_of,
                    freshness_window=timedelta(
                        seconds=2 * SupervisedOperatingRunner.EVALUATION_CADENCE_SECONDS
                    ),
                    has_active_work=_has_open_paper_activity(crypto_state),
                )
            )
        for kalshi_state in kalshi_states:
            latest_kalshi_reconciled_at = max(
                (item.reconciled_at for item in kalshi_state.reconciliations), default=None
            )
            cross_venue_continuation_health[kalshi_state.account.account_id] = _jsonable(
                compute_paper_continuation_health(
                    latest_terminal_status=(
                        latest_kalshi_task_result.status if latest_kalshi_task_result else None
                    ),
                    latest_terminal_at=(
                        latest_kalshi_task_result.completed_at
                        if latest_kalshi_task_result
                        else None
                    ),
                    latest_continuation_at=latest_kalshi_reconciled_at,
                    now=mark_as_of,
                    freshness_window=timedelta(
                        seconds=2 * SupervisedOperatingRunner.EVALUATION_CADENCE_SECONDS
                    ),
                    has_active_work=_has_open_paper_activity(kalshi_state),
                )
            )
        # Strategy Evaluation History & Promotion Lineage v1: the recurring
        # catalog's own durably retained evaluation/promotion lineage,
        # keyed by strategy_id -- read-only here (never registers/promotes
        # anything). Freshness is computed here, against real datetime
        # objects, BEFORE jsonification -- build_operator_paper_strategy_
        # registry only ever reads an already-computed label, mirroring how
        # it already receives continuation_health pre-computed by this same
        # caller rather than doing its own time math.
        internal_evaluations_repository = PostgresInternalStrategyEvaluationRepository(engine)
        internal_promotions_repository = PostgresInternalStrategyPromotionRepository(engine)
        internal_evaluation_history_raw = internal_strategy_evaluation_registry(
            evaluations=internal_evaluations_repository,
            promotions=internal_promotions_repository,
            strategy_ids=tuple(
                item.strategy_id for item in canonical_strategy_benchmarks(created_at=mark_as_of)
            ),
        )
        for _entry in internal_evaluation_history_raw.values():
            _latest = cast(
                "InternalStrategyEvaluationRecord | None", _entry.get("latest_evaluation")
            )
            _entry["freshness"] = evaluate_freshness(
                _latest.evaluated_at if _latest is not None else None, now=mark_as_of
            ).value
            # Part 13: "a compact history drill-down or bounded recent-
            # evaluations list is sufficient" -- never the full retained
            # history in one console payload.
            _entry["evaluations"] = cast(tuple[object, ...], _entry["evaluations"])[:3]

        # Repeatable Strategy PAPER Evidence v1: durable, generation-aware
        # trial lineage per candidate slot -- read-only here (never opens/
        # closes a trial). strategy_version uses the identical
        # promoted_trial_slot_version convention the recurring task itself
        # uses to compute a slot's trial identity -- see that function's
        # own docstring for why sharing this one computation, rather than
        # each caller re-deriving "+1" independently, is load-bearing.
        strategy_paper_trials_repository = PostgresStrategyPaperTrialRepository(engine)
        strategy_paper_trial_closures_repository = PostgresStrategyPaperTrialClosureRepository(
            engine
        )
        trial_lineage_by_strategy_id_raw = {
            definition.strategy_id: strategy_paper_trial_lineage(
                trials=strategy_paper_trials_repository,
                closures=strategy_paper_trial_closures_repository,
                strategy_id=definition.strategy_id,
                strategy_version=promoted_trial_slot_version(definition_version=definition.version),
                instrument_id=definition.instrument_ids[0],
            )
            for definition in canonical_strategy_benchmarks(created_at=mark_as_of)
        }

        strategy_registry = build_operator_paper_strategy_registry(
            candidates=cast(
                tuple[object, ...],
                _jsonable(canonical_strategy_benchmarks(created_at=mark_as_of)),
            ),
            latest_task_result=_jsonable(latest_strategy_task_result),
            open_trial_order_by_strategy_id=open_trial_order_by_strategy_id,
            account_last_reconciled_at=account_last_reconciled_at,
            continuation_health=_jsonable(strategy_continuation_health),
            internal_evaluation_history=cast(
                dict[str, object], _jsonable(internal_evaluation_history_raw)
            ),
            trial_lineage_by_strategy_id=cast(
                dict[str, object], _jsonable(trial_lineage_by_strategy_id_raw)
            ),
        )

        paper_operations = build_operator_paper_operations(
            crypto_accounts=cast(tuple[object, ...], _jsonable(crypto_states)),
            kalshi_accounts=cast(tuple[object, ...], _jsonable(kalshi_states)),
            external_instructions=cast(tuple[object, ...], _jsonable(external)),
            crypto_capability=_jsonable(crypto_paper_repository.latest_capability()),
            kalshi_capability=_jsonable(kalshi_operating_repository.latest_capability()),
            paper_activation=_jsonable(operating_cycle_repository.workspace()).get(
                "paper_activation"
            ),
            position_marks=position_marks,
            unrealized_totals=unrealized_totals,
            continuation_health=cross_venue_continuation_health,
        )
        # Strategy PAPER Trial Outcome Assessment v1: a concise, read-only
        # view of the retained outcome evidence. Never recomputed here and
        # never read by any PAPER, eligibility, ranking, or promotion path.
        outcome_report = strategy_trial_outcome_report(
            assessments=PostgresStrategyTrialOutcomeAssessmentRepository(engine).assessments(),
            closures=strategy_paper_trial_closures_repository.all_closures(),
            active_trials=strategy_paper_trials_repository.active_trials(),
        )
        outcome_assessments = cast(
            "tuple[StrategyTrialOutcomeAssessment, ...]", outcome_report["assessments"]
        )
        trial_outcome_assessments = {
            "policy_version": outcome_report["policy_version"],
            "minimum_sample_size": outcome_report["minimum_sample_size"],
            "assessment_count": outcome_report["assessment_count"],
            "status_counts": outcome_report["status_counts"],
            "provenance_bound_sample_count": outcome_report["provenance_bound_sample_count"],
            "closed_trials_awaiting_assessment": outcome_report[
                "closed_trials_awaiting_assessment"
            ],
            "recent": tuple(
                strategy_trial_outcome_summary(item) for item in outcome_assessments[-10:]
            ),
            "authority": "NONE",
            "live_authority": "NONE",
            "warning": outcome_report["warning"],
        }
        return {
            **paper_operations,
            "strategy_registry": strategy_registry,
            "trial_outcome_assessments": _jsonable(trial_outcome_assessments),
        }

    def research_operations_value() -> dict[str, object]:
        # Operator Console Home Performance v1: request-scoped memoizing
        # wrappers, not the real repository/knowledge_source directly --
        # see this module's own comment block above for the measured
        # trace. research_operations_workspace()/research_review_queue()
        # receive zero line changes; every classification this reports
        # (proposal state, duplicate decision, review-queue status)
        # remains exactly as live and correct as before.
        workspace = research_operations_workspace(
            _MemoizedResearchOperationsRepository(research_operations_repository),
            _MemoizedResearchKnowledgeSource(research_operations_knowledge),
        )
        workspace["generated_crypto_challenger"] = {
            "refinements": crypto_stateful_research_repository.refinements(),
            "specifications": crypto_stateful_research_repository.specifications(),
            "execution_histories": crypto_stateful_research_repository.execution_histories(),
            "experiment_runs": crypto_stateful_research_repository.experiment_runs(),
            "current_signals": crypto_stateful_research_repository.current_signals(),
            "automatic_paper_action": False,
            "authority": "NONE",
            "live_authority": "NONE",
        }
        return cast(
            dict[str, object],
            _jsonable(workspace),
        )

    @app.get("/api/v1/research-operations/workspace")
    def research_operations_read() -> dict[str, object]:
        """Retained agenda, proposals, budgets, and recommendations only."""

        return _envelope(research_operations_value(), capability="RESEARCH_OPERATIONS")

    @app.get("/api/v1/research-operations/generated-crypto-challenger")
    def generated_crypto_challenger_read() -> dict[str, object]:
        """Exact refinement, run, and current-signal evidence with zero authority."""

        return _envelope(
            research_operations_value()["generated_crypto_challenger"],
            capability="RESEARCH_OPERATIONS",
        )

    @app.post("/api/v1/research-operations/generator-runs")
    def research_proposal_generation_run(
        body: ResearchProposalGenerationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            source = RetainedResearchAgendaSource(engine)
            agenda = research_operations_repository.agenda_items()
            result = GenerateResearchProposals(
                research_operations_repository,
                research_operations_knowledge,
            ).execute(
                generator_run_id=body.generator_run_id,
                generated_at=datetime.now(tz=UTC),
                actor=body.actor,
                context=source.generation_context(agenda_items=tuple(agenda)),
                maximum_proposals=body.maximum_proposals,
                origin=ResearchProposalOrigin(body.origin),
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "proposal generation rejected") from error
        return _envelope(
            {
                "generator_run": result.generator_run,
                "proposals": result.proposals,
                "priorities": result.priorities,
                "feasibilities": result.feasibilities,
                "idempotency_key": idempotency_key,
                "idempotent_replay": result.idempotent_replay,
                "proposals_approved": 0,
                "experiments_run": 0,
                "provider_requests": 0,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.get("/api/v1/research-operations/generator-runs/{generator_run_id}")
    def research_proposal_generator_run_read(
        generator_run_id: str, request: Request
    ) -> dict[str, object]:
        value = research_operations_repository.generator_run(generator_run_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_GENERATOR_RUN_NOT_FOUND",
                "the exact retained proposal-generator run was not found",
                entity_type="ResearchProposalGeneratorRun",
                entity_id=generator_run_id,
            )
        return _envelope(
            {"generator_run": value, "authority": "NONE"},
            capability="RESEARCH_OPERATIONS",
        )

    @app.get("/api/v1/research-operations/priorities/{proposal_id}")
    def research_proposal_priority_read(proposal_id: str, request: Request) -> dict[str, object]:
        value = research_operations_repository.proposal_priority(proposal_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_PROPOSAL_PRIORITY_NOT_FOUND",
                "the exact retained proposal priority was not found",
                entity_type="ResearchProposalPriority",
                entity_id=proposal_id,
            )
        return _envelope({"priority": value, "authority": "NONE"}, capability="RESEARCH_OPERATIONS")

    @app.get("/api/v1/research-operations/feasibilities/{proposal_id}")
    def research_proposal_feasibility_read(proposal_id: str, request: Request) -> dict[str, object]:
        value = research_operations_repository.proposal_feasibility(proposal_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_PROPOSAL_FEASIBILITY_NOT_FOUND",
                "the exact retained proposal feasibility decision was not found",
                entity_type="ResearchProposalFeasibility",
                entity_id=proposal_id,
            )
        return _envelope(
            {"feasibility": value, "authority": "NONE"},
            capability="RESEARCH_OPERATIONS",
        )

    @app.get("/api/v1/research-operations/review-queue")
    def research_proposal_review_queue_read() -> dict[str, object]:
        return _envelope(
            {
                "items": research_review_queue(
                    research_operations_repository, research_operations_knowledge
                ),
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS",
        )

    @app.get("/api/v1/research-operations/agenda/{agenda_item_id}")
    def research_agenda_item_read(agenda_item_id: str, request: Request) -> dict[str, object]:
        value = research_operations_repository.agenda_item(agenda_item_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_AGENDA_ITEM_NOT_FOUND",
                "the exact retained research agenda item was not found",
                entity_type="ResearchAgendaItem",
                entity_id=agenda_item_id,
            )
        return _envelope(
            {"agenda_item": value, "authority": "NONE", "live_authority": "NONE"},
            capability="RESEARCH_OPERATIONS",
        )

    @app.get("/api/v1/research-operations/proposals/{proposal_id}")
    def governed_research_proposal_read(proposal_id: str, request: Request) -> dict[str, object]:
        value = research_operations_repository.proposal(proposal_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_PROPOSAL_NOT_FOUND",
                "the exact retained governed research proposal was not found",
                entity_type="ResearchProposal",
                entity_id=proposal_id,
            )
        workspace = research_operations_value()
        retained = next(
            (
                item
                for item in cast(list[dict[str, object]], workspace.get("proposals", []))
                if cast(dict[str, object], item.get("proposal", {})).get("proposal_id")
                == proposal_id
            ),
            None,
        )
        return _envelope(
            retained or {"proposal": value, "authority": "NONE"},
            capability="RESEARCH_OPERATIONS",
        )

    @app.post("/api/v1/research-operations/proposals")
    def governed_research_proposal_create(
        body: GovernedResearchProposalRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            existing = research_operations_repository.proposal(body.proposal_id)
            proposal = ResearchProposal.create(
                proposal_id=body.proposal_id,
                version=body.version,
                agenda_item_id=body.agenda_item_id,
                venue=ResearchVenue(body.venue),
                hypothesis=body.hypothesis,
                expected_mechanism=body.expected_mechanism,
                instruments=tuple(body.instruments),
                proposed_family=body.proposed_family,
                definition_kind=body.definition_kind,
                strategy_definition=tuple(body.strategy_definition),
                exact_change_from_prior=body.exact_change_from_prior,
                prior_research_ids=tuple(body.prior_research_ids),
                required_data_ids=tuple(body.required_data_ids),
                experiment_configuration=tuple(body.experiment_configuration),
                validation_windows=tuple(body.validation_windows),
                cost_assumptions=tuple(body.cost_assumptions),
                success_criteria=tuple(body.success_criteria),
                failure_criteria=tuple(body.failure_criteria),
                maximum_experiment_runs=body.maximum_experiment_runs,
                origin=ResearchProposalOrigin(body.origin),
                actor=body.actor,
                warnings=tuple(body.warnings),
                created_at=(existing.created_at if existing is not None else datetime.now(tz=UTC)),
            )
            retained, replay = CreateResearchProposal(research_operations_repository).execute(
                proposal
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "research proposal rejected") from error
        return _envelope(
            {
                "proposal": retained,
                "idempotent_replay": replay,
                "state": ResearchProposalState.DRAFT,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.post("/api/v1/research-operations/proposals/{proposal_id}/review")
    def governed_research_proposal_review(
        proposal_id: str,
        body: GovernedResearchReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            review, duplicate, replay = ReviewResearchProposal(
                research_operations_repository,
                research_operations_knowledge,
            ).execute(
                proposal_id=proposal_id,
                target_state=ResearchProposalState(body.target_state),
                actor=body.actor,
                reason=body.reason,
                confirmation=body.confirmation,
                reviewed_at=datetime.now(tz=UTC),
                idempotency_key=idempotency_key,
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "research review rejected") from error
        return _envelope(
            {
                "review": review,
                "duplicate_decision": duplicate,
                "idempotent_replay": replay,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.post("/api/v1/research-operations/proposals/{proposal_id}/run")
    def governed_research_proposal_run(
        proposal_id: str,
        body: GovernedResearchRunRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            result = RunGovernedResearchExperiment(
                research_operations_repository,
                GovernedResearchExecutor(
                    ledger=ExistingResearchLedgerExecutor(research_integrity_repository),
                    operational=FirstGeneratedOperationalMethodExecutor(
                        research_operations_repository,
                        PostgresCurrentOpportunityOperationalEvidenceSource(engine),
                    ),
                    outcome_aggregate=research_outcome_aggregate_executor,
                ),
                research_operations_knowledge,
            ).execute(
                proposal_id=proposal_id,
                run_id=body.run_id,
                actor=body.actor,
                confirmation=body.confirmation,
                executed_at=datetime.now(tz=UTC),
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "research experiment rejected") from error
        return _envelope(
            {
                "outcome": result.outcome,
                "recommendation": result.recommendation,
                "budget": result.budget,
                "idempotent_replay": result.idempotent_replay,
                "provider_writes": 0,
                "financial_mutations": 0,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.post("/api/v1/research-operations/experiment-queue")
    def research_experiment_enqueue(
        body: ResearchExperimentEnqueueRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            result = EnqueueResearchExperiment(research_operations_repository).execute(
                queue_id=body.queue_id,
                proposal_id=body.proposal_id,
                run_id=body.run_id,
                actor=body.actor,
                idempotency_key=idempotency_key,
                enqueued_at=datetime.now(tz=UTC),
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "experiment enqueue rejected") from error
        return _envelope(
            {
                "queue_item": result.item,
                "queue_event": result.event,
                "idempotent_replay": result.idempotent_replay,
                "authority": "EXPERIMENT_ONLY",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.get("/api/v1/research-operations/experiment-queue/{queue_id}")
    def research_experiment_queue_read(queue_id: str, request: Request) -> dict[str, object]:
        item = research_operations_repository.queue_item(queue_id)
        if item is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_QUEUE_ITEM_NOT_FOUND",
                "the exact retained experiment queue item was not found",
                entity_type="ResearchExperimentQueueItem",
                entity_id=queue_id,
            )
        return _envelope(
            {
                "queue_item": item,
                "events": research_operations_repository.queue_events(queue_id),
                "authority": "EXPERIMENT_ONLY",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS",
        )

    @app.post("/api/v1/research-operations/experiment-queue/{queue_id}/cancel")
    def research_experiment_queue_cancel(
        queue_id: str,
        body: ResearchExperimentQueueActionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            event, replay = CancelQueuedResearchExperiment(research_operations_repository).execute(
                queue_id=queue_id,
                actor=body.actor,
                reason=body.reason,
                idempotency_key=idempotency_key,
                canceled_at=datetime.now(tz=UTC),
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "queue cancellation rejected") from error
        return _envelope(
            {
                "queue_event": event,
                "idempotent_replay": replay,
                "authority": "EXPERIMENT_ONLY",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.post("/api/v1/research-operations/experiment-queue/{queue_id}/execute")
    def research_experiment_queue_execute(
        queue_id: str,
        body: ResearchExperimentQueueActionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            result = ExecuteApprovedResearchQueueItem(
                research_operations_repository,
                GovernedResearchExecutor(
                    ledger=ExistingResearchLedgerExecutor(research_integrity_repository),
                    operational=FirstGeneratedOperationalMethodExecutor(
                        research_operations_repository,
                        PostgresCurrentOpportunityOperationalEvidenceSource(engine),
                    ),
                    outcome_aggregate=research_outcome_aggregate_executor,
                ),
                research_operations_knowledge,
            ).execute(
                queue_id=queue_id,
                actor=body.actor,
                idempotency_key=idempotency_key,
                executed_at=datetime.now(tz=UTC),
            )
        except ResearchOperationsError as error:
            raise _http_error(request, 409, str(error), "queue execution rejected") from error
        return _envelope(
            {
                "queue_item": result.item,
                "queue_event": result.event,
                "outcome": result.governed_result.outcome,
                "recommendation": result.governed_result.recommendation,
                "budget": result.governed_result.budget,
                "idempotent_replay": result.idempotent_replay,
                "provider_requests": 0,
                "financial_mutations": 0,
                "authority": "EXPERIMENT_ONLY",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="RESEARCH_OPERATIONS_MUTATION",
        )

    @app.get("/api/v1/research-operations/recommendations/{recommendation_id}")
    def research_recommendation_read(recommendation_id: str, request: Request) -> dict[str, object]:
        value = next(
            (
                item
                for item in research_operations_repository.recommendations()
                if item.recommendation_id == recommendation_id
            ),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "RESEARCH_RECOMMENDATION_NOT_FOUND",
                "the exact retained recommendation was not found",
                entity_type="ResearchRecommendation",
                entity_id=recommendation_id,
            )
        return _envelope(
            {"recommendation": value, "authority": "NONE", "live_authority": "NONE"},
            capability="RESEARCH_OPERATIONS",
        )

    def overview_extensions_value() -> dict[str, object]:
        """Additive Home summaries for the four newer subsystems. Reuses
        the identical cheap ``coverage()`` aggregates, the identical
        read-only ``PlanFollowUpObservations``, and the identical
        already-reviewed ``resolve_options_capture_due_decision`` the
        dedicated Decision Ledger/Follow-Up/Options Research/Portfolio
        routes above use -- never a second computation of eligibility,
        classification, follow-up, or scheduling truth.

        Operator Console Home Performance v1: the ``PlanFollowUpObservations``
        construction below now uses the identical request-scoped
        ``_PrefetchedDecisions``/``_PrefetchedProjections``/
        ``_MemoizedCryptoUniverseObservations`` wrappers
        ``operator_console_followups`` already uses, instead of the real,
        unwrapped repositories -- see this module's own "Operator Console
        Follow-Up Performance v1" and "Operator Console Home Performance
        v1" comment blocks above for the measured trace this fixes.
        ``PlanFollowUpObservations`` itself receives zero line changes."""

        from apex.adapters.options_research_capture import resolve_options_capture_due_decision

        now = datetime.now(tz=UTC)
        decision_coverage = cast(
            dict[str, object], _jsonable(counterfactual_ledger_repository.coverage())
        )
        observations = counterfactual_followup_repository.observations()
        already_observed_ids = {item.observation_id for item in observations}
        decisions = counterfactual_ledger_repository.decisions()
        followup_coverage = cast(
            dict[str, object],
            _jsonable(
                counterfactual_followup_repository.coverage(
                    decisions=decisions, observations=observations
                )
            ),
        )
        projections_by_id = shared_opportunity_repository.projections_by_id(
            {item.subject_projection_id for item in decisions}
        )
        plan_service = PlanFollowUpObservations(
            _PrefetchedDecisions(decisions),
            _PrefetchedProjections(projections_by_id),
            _CryptoFollowUpPriceSource(
                cast(
                    PostgresCryptoUniverseRepository,
                    _MemoizedCryptoUniverseObservations(crypto_universe_repository),
                )
            ),
        )
        # Counterfactual Forward Outcome Temporal Integrity v1: the current
        # policy is a per-venue selection now, so this plans the whole
        # selection (one bounded read-only plan per distinct version, each
        # scoped to its own venues) rather than silently picking one version
        # and hiding the other's venues from Home's backlog figure.
        plan = plan_current_followup_selection(
            plan_service, as_of=now, already_observed=already_observed_ids
        )
        followup_plan_summary = cast(
            dict[str, object],
            _jsonable(
                {
                    "as_of": plan.as_of,
                    "policy_version": " · ".join(plan.policy_versions),
                    "policy_versions": list(plan.policy_versions),
                    "policy_version_by_venue": [
                        list(pair) for pair in plan.policy_version_by_venue
                    ],
                    "considered_decisions": plan.considered_decisions,
                    "considered_pairs": plan.considered_pairs,
                    "state_counts": plan.state_counts,
                }
            ),
        )
        # A compact, Home-appropriate slice of the same already-retained
        # cycle/cursor evidence the dedicated Follow-Up page shows in full
        # (see ``operator_console_followups`` below) -- reused unchanged,
        # never a second computation, so "is research progressing?" is
        # answerable from Home too without a second page load.
        from apex.adapters.operating_cycle_tasks import (
            COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
        )

        followup_progress = build_followup_progress(
            plan_state_counts=plan.state_counts,
            latest_cycle_task=latest_effective_followup_cycle_task(),
            scan_cursors=cast(
                list[dict[str, object]],
                _jsonable(
                    [
                        cursor
                        for policy_version in CURRENT_FOLLOWUP_POLICY_VERSIONS
                        if (
                            cursor := counterfactual_followup_repository.scan_cursor_diagnostics(
                                policy_version=policy_version
                            )
                        )
                        is not None
                    ]
                ),
            ),
            kalshi_request_budget=COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
        )

        options_plan, _window_policy, _sessions, options_decision = (
            resolve_options_capture_due_decision(
                quote_repository=options_quote_intelligence_repository,
                checkpoint_repository=options_research_operations_repository,
                as_of=now,
            )
        )
        checkpoints = (
            options_research_operations_repository.checkpoints_for_plan(options_plan.plan_id)
            if options_plan is not None
            else ()
        )
        options_summary = build_operator_options_research(
            cast(list[dict[str, object]], _jsonable(checkpoints)),
            target_session_count=IV_RANK_MINIMUM_HISTORY,
            capture_task=cast(dict[str, object], _jsonable(options_decision)),
        )

        snapshot = portfolio_state_repository.latest_portfolio_snapshot()
        mandate = investment_mandate_repository.active_mandate(
            DEFAULT_MANDATE_LINEAGE_ID, as_of=now
        )
        evaluations = investment_mandate_repository.all_evaluations()
        portfolio_summary = build_operator_portfolio_mandate(
            portfolio_snapshot=cast(dict[str, object] | None, _jsonable(snapshot)),
            account_balances=(),
            positions=(),
            mandate=cast(dict[str, object] | None, _jsonable(mandate)),
            mandate_lineage_versions=(),
            latest_evaluations=cast(list[dict[str, object]], _jsonable(evaluations)),
        )

        return build_operator_overview_extensions(
            decision_coverage=decision_coverage,
            followup_coverage=followup_coverage,
            followup_plan_summary=followup_plan_summary,
            followup_progress=followup_progress,
            options_summary=options_summary,
            portfolio_summary=portfolio_summary,
        )

    @app.get("/api/v1/operator-console/home")
    def operator_console_home() -> dict[str, object]:
        """Default operator summary sourced only from the retained operating brief."""

        operating, shared, cross_venue, performance = operator_console_sources()
        paper = operator_console_paper()
        attention = build_operator_attention(
            operating=operating,
            shared=shared,
            cross_venue=cross_venue,
            performance=performance,
            paper=paper,
            research_operations=research_operations_value(),
        )
        home = build_operator_home(
            operating=operating,
            attention=attention,
            paper=paper,
            performance=performance,
            allocation=cross_venue,
        )
        return _envelope(
            {**home, **overview_extensions_value()},
            capability="OPERATOR_READ_ONLY",
        )

    @app.get("/api/v1/operator-console/attention")
    def operator_console_attention(
        request: Request,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        """Bounded deterministic queue of retained states requiring review."""

        _page(request, limit, offset)
        operating, shared, cross_venue, performance = operator_console_sources()
        values = build_operator_attention(
            operating=operating,
            shared=shared,
            cross_venue=cross_venue,
            performance=performance,
            paper=operator_console_paper(),
            research_operations=research_operations_value(),
        )
        return _envelope(
            {
                "items": values[offset : offset + limit],
                "total": len(values),
                "limit": limit,
                "offset": offset,
                "empty_state": "NO_OPERATOR_ATTENTION" if not values else None,
                "authority": "NONE",
                "live_authority": "NONE",
            },
            capability="OPERATOR_READ_ONLY",
        )

    @app.get("/api/v1/operator-console/opportunities")
    def operator_console_opportunities(
        request: Request,
        venue: str = "ALL",
        eligibility: str = "ALL",
        priority: str = "ALL",
        allocation: str = "ALL",
        paper_readiness: str = "ALL",
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        """Bounded server-filtered current Opportunity inbox."""

        _page(request, limit, offset)
        _, shared, cross_venue, _ = operator_console_sources()
        try:
            value = build_operator_opportunities(
                shared,
                cross_venue=cross_venue,
                venue=venue,
                eligibility=eligibility,
                priority=priority,
                allocation=allocation,
                paper_readiness=paper_readiness,
                query=query,
                limit=limit,
                offset=offset,
            )
        except OperatorConsoleFilterError as error:
            raise _http_error(request, 400, str(error), "invalid operator inbox filter") from error
        decisions_by_subject = {
            item.subject_projection_id: item
            for item in counterfactual_ledger_repository.decisions()
        }
        value = enrich_opportunities_with_decisions(
            value,
            decisions_by_subject=cast(
                dict[str, dict[str, object]], _jsonable(decisions_by_subject)
            ),
        )
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/strategy-library")
    def operator_console_strategy_library() -> dict[str, object]:
        """Current retained Crypto and Kalshi method records; no provider refresh."""

        items = RetainedSharedOpportunitySource(engine).strategy_library()
        return _envelope(
            {
                "items": items,
                "total": len(items),
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
            },
            capability="OPERATOR_READ_ONLY",
        )

    @app.get("/api/v1/operator-console/allocation")
    def operator_console_allocation() -> dict[str, object]:
        """Exact hypothetical allocation review with no authorization controls."""

        _, _, cross_venue, _ = operator_console_sources()
        return _envelope(build_operator_allocation(cross_venue), capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/paper")
    def operator_console_paper_workspace() -> dict[str, object]:
        """Consolidated but explicitly separated internal/external PAPER evidence."""

        return _envelope(operator_console_paper(), capability="OPERATOR_PAPER_READ_ONLY")

    @app.get("/api/v1/operator-console/external-strategies")
    def operator_console_external_strategies() -> dict[str, object]:
        """External Strategy Evaluation v1's own registry -- deliberately
        separate from ``/api/v1/operator-console/paper``'s ``strategy_
        registry`` field above: that one reflects the LIVE recurring
        ``STRATEGY_PAPER_TRIAL_EXECUTION`` task's fixed four-candidate
        catalog; this one reflects operator-registered external strategies,
        which that task never reads and can never place a PAPER order for
        (see ``domain.external_strategy``'s module docstring)."""

        definitions = PostgresExternalStrategyDefinitionRepository(engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(engine)
        snapshot = _jsonable(
            external_strategy_registry_snapshot(definitions=definitions, evaluations=evaluations)
        )
        return _envelope(
            build_operator_external_strategy_registry(snapshot), capability="OPERATOR_READ_ONLY"
        )

    @app.get("/api/v1/operator-console/external-strategies/{strategy_id}")
    def operator_console_external_strategy_detail(
        strategy_id: str, request: Request
    ) -> dict[str, object]:
        """Full retained lineage for one external strategy: every version,
        every evaluation. 404 if no version was ever registered."""

        definitions = PostgresExternalStrategyDefinitionRepository(engine)
        evaluations = PostgresExternalStrategyEvaluationRepository(engine)
        detail = external_strategy_detail(
            definitions=definitions, evaluations=evaluations, strategy_id=strategy_id
        )
        if not detail["versions"]:
            raise _http_error(
                request,
                404,
                "EXTERNAL_STRATEGY_NOT_FOUND",
                "no version of this external strategy is retained",
                entity_type="StrategyDefinition",
                entity_id=strategy_id,
            )
        return _envelope(_jsonable(detail), capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/learning")
    def operator_console_learning() -> dict[str, object]:
        """Descriptive retained attribution and health evidence only."""

        _, _, _, performance = operator_console_sources()
        return _envelope(build_operator_learning(performance), capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/research-operations")
    def operator_console_research_operations() -> dict[str, object]:
        """Compact governed research agenda and proposal control plane."""

        return _envelope(
            build_operator_research_operations(research_operations_value()),
            capability="RESEARCH_OPERATIONS",
        )

    @app.get("/api/v1/operator-console/live-readiness")
    def operator_console_live_readiness() -> dict[str, object]:
        """Sanitized provider and LIVE-readiness evidence; never an execution surface."""

        workspace = cast(
            dict[str, object],
            _jsonable(live_readiness_workspace(live_readiness_repository)),
        )
        operations = cast(
            dict[str, object],
            _jsonable(operational_workspace(operational_reliability_repository)),
        )
        coinbase = cast(
            dict[str, object],
            _jsonable(coinbase_read_only_workspace(coinbase_read_only_repository)),
        )
        kalshi = cast(
            dict[str, object],
            _jsonable(kalshi_read_only_workspace(kalshi_read_only_repository)),
        )
        preflight = cast(
            dict[str, object],
            _jsonable(
                live_preflight_workspace(
                    _MemoizedLivePreflightInvalidations(live_preflight_repository),
                    as_of=datetime.now(tz=UTC),
                )
            ),
        )
        return _envelope(
            build_operator_live_readiness(
                workspace,
                operational=operations,
                coinbase_read_only=coinbase,
                kalshi_read_only=kalshi,
                live_preflight=preflight,
            ),
            capability="LIVE_READINESS_READ_ONLY",
        )

    @app.get("/api/v1/operator-console/broad-market")
    def operator_console_broad_market() -> dict[str, object]:
        """Bounded broad-market reference summary; never an execution surface."""

        summary = _broad_market_reference_summary(
            security_reference_repository,
            broad_equity_repository,
            options_intelligence_repository,
            research_evidence_repository,
            security_identity_repository,
            historical_market_data_repository,
            genuine_multi_asset_repository,
            historical_replay_repository,
            corporate_fundamentals_repository,
        )
        return _envelope(build_operator_broad_market(summary), capability="RESEARCH_ONLY")

    @app.get("/api/v1/operator-console/live-preflight")
    def operator_console_live_preflight() -> dict[str, object]:
        """Exact retained non-executing preflight evidence."""

        workspace = cast(
            dict[str, object],
            _jsonable(
                live_preflight_workspace(
                    _MemoizedLivePreflightInvalidations(live_preflight_repository),
                    as_of=datetime.now(tz=UTC),
                )
            ),
        )
        return _envelope(
            build_operator_live_preflight(workspace),
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    def counterfactual_sources() -> tuple[
        list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]
    ]:
        """Every retained decision/classification/follow-up observation --
        the same bounded 'read everything, filter and paginate in Python'
        shape ``operator_console_sources`` already uses for Opportunities."""

        return (
            cast(
                list[dict[str, object]],
                _jsonable(counterfactual_ledger_repository.decisions()),
            ),
            cast(
                list[dict[str, object]],
                _jsonable(counterfactual_ledger_repository.classifications()),
            ),
            cast(
                list[dict[str, object]],
                _jsonable(counterfactual_followup_repository.observations()),
            ),
        )

    def latest_effective_followup_cycle_task() -> dict[str, object] | None:
        """The most recently retained COMPLETED-or-REUSED
        ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` task result, if any -- the
        identical bounded 'most recent 15 cycles' scan
        ``operator_console_operations`` already performs, narrowed to one
        task type so the Follow-Up page can show the same already-retained
        cycle diagnostics (its own warning-code facts) without a second
        sync, a broader query, or any change to that existing route.
        ``None`` when this task has never once completed or been reused in
        any retained cycle -- an honest empty state, never a fabricated
        zero."""

        cycles = sorted(operating_cycle_repository.cycles(), key=lambda item: item.created_at)
        for cycle in reversed(cycles[-15:]):
            rows = cast(
                list[dict[str, object]],
                _jsonable(operating_cycle_repository.task_results(cycle.cycle_id)),
            )
            match = next(
                (
                    row
                    for row in rows
                    if row.get("task_type") == "COUNTERFACTUAL_FOLLOWUP_REFRESH"
                    and row.get("status") in ("COMPLETED", "REUSED")
                ),
                None,
            )
            if match is not None:
                return match
        return None

    @app.get("/api/v1/operator-console/decisions")
    def operator_console_decisions(
        request: Request,
        venue: str = "ALL",
        disposition: str = "ALL",
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        """Bounded, filterable view over the stable CounterfactualDecision ledger."""

        _page(request, limit, offset)
        decisions, classifications, observations = counterfactual_sources()
        by_decision: dict[str, list[dict[str, object]]] = {}
        for item in classifications:
            by_decision.setdefault(str(item.get("decision_id")), []).append(item)
        followup_ids = tuple({str(item.get("decision_id")) for item in observations})
        try:
            value = build_operator_decisions(
                decisions,
                classifications_by_decision=by_decision,
                followup_decision_ids=followup_ids,
                current_classifier_version=CURRENT_CLASSIFIER_VERSION,
                known_classifier_versions=KNOWN_CLASSIFIER_VERSIONS,
                venue=venue,
                disposition=disposition,
                query=query,
                limit=limit,
                offset=offset,
            )
        except OperatorConsoleFilterError as error:
            raise _http_error(request, 400, str(error), "invalid decision ledger filter") from error
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/decisions/{decision_id}")
    def operator_console_decision_detail(decision_id: str, request: Request) -> dict[str, object]:
        """The one detail bundle every decision-linked evidence_href (from
        the Decision Ledger, Opportunities, and Follow-Up views alike)
        resolves to: the stable decision, every classifier version's
        independent interpretation of it, and every retained follow-up
        observation."""

        decision = counterfactual_ledger_repository.decision(decision_id)
        if decision is None:
            raise _http_error(
                request,
                404,
                "COUNTERFACTUAL_DECISION_NOT_FOUND",
                "the exact retained decision was not found",
                entity_type="CounterfactualDecision",
                entity_id=decision_id,
            )
        classifications = counterfactual_ledger_repository.classifications_for_decision(decision_id)
        followups = counterfactual_followup_repository.observations_for_decision(decision_id)
        value = build_operator_decision_detail(
            cast(dict[str, object], _jsonable(decision)),
            classifications=cast(list[dict[str, object]], _jsonable(classifications)),
            followups=cast(list[dict[str, object]], _jsonable(followups)),
        )
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/followups")
    def operator_console_followups(
        request: Request,
        policy_version: str = "ALL",
        baseline_kind: str = "ALL",
        horizon_kind: str = "ALL",
        venue: str = "ALL",
        status: str = "ALL",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        """Every retained V1/V2 follow-up observation plus the current
        policy's own read-only due/pending backlog. Calls only
        ``PlanFollowUpObservations`` -- explicitly read-only, makes no
        provider request (see its own docstring) -- never
        ``SyncFollowUpObservations``, the service that actually performs a
        live Kalshi/Coinbase read; opening this page can never trigger a
        follow-up sync. Fairness/progress evidence (cycle diagnostics, scan
        cursor) is read from the identical already-retained recurring-cycle
        and cursor-table rows the CLI/operating cycle themselves wrote --
        this route computes none of it and triggers no sync or write of its
        own to produce it.

        Operator Console Follow-Up Performance v1: ``PlanFollowUpObservations``
        is still the one and only place this due/pending backlog is
        computed -- same class, same ``execute()`` call shape, same
        selection/eligibility logic, zero lines of it changed -- but its
        three dependencies are now request-scoped prefetch/memoizing
        wrappers (``_PrefetchedDecisions``/``_PrefetchedProjections``/
        ``_MemoizedCryptoUniverseObservations``, defined above) instead of
        the real per-call-unbounded repositories, so this now costs one
        bounded read of each dependency instead of hundreds. See this
        module's own "Operator Console Follow-Up Performance v1" comment
        block above for the measured before/after trace."""

        from apex.adapters.operating_cycle_tasks import (
            COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
        )

        _page(request, limit, offset)
        observations = counterfactual_followup_repository.observations()
        already_observed_ids = {item.observation_id for item in observations}
        decisions = counterfactual_ledger_repository.decisions()
        decisions_by_id = {item.decision_id: item for item in decisions}
        # CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
        # Remediation v1: one bounded read of the (today, tiny -- one row
        # per retained observation, at most) evidence-quality assessment
        # table, keyed by ``observation_id``. Deliberately does not touch
        # ``PlanFollowUpObservations``'s own selection logic -- this is an
        # unrelated, independent, additive read.
        #
        # Securities Follow-Up Temporal Integrity v1: each observation shows
        # the verdict under its own venue's canonical policy version (V2 for
        # SECURITIES, V1 for CRYPTO/KALSHI).  A SECURITIES row with no V2
        # assessment yet honestly reads ``UNASSESSED`` -- never its V1
        # verdict, which does not check terminal-evidence timing.
        canonical_version_by_observation_id = {
            item.observation_id: current_followup_evidence_quality_policy_version(item.venue)
            for item in observations
        }
        evidence_assessments_by_observation_id = {
            item.observation_id: item
            for version in sorted(set(canonical_version_by_observation_id.values()))
            for item in counterfactual_followup_repository.assessments(
                assessment_policy_version=version
            )
            if canonical_version_by_observation_id.get(item.observation_id) == version
        }
        projections_by_id = shared_opportunity_repository.projections_by_id(
            {item.subject_projection_id for item in decisions}
        )
        plan_service = PlanFollowUpObservations(
            _PrefetchedDecisions(decisions),
            _PrefetchedProjections(projections_by_id),
            _CryptoFollowUpPriceSource(
                # _CryptoFollowUpPriceSource's own __init__ names the
                # concrete PostgresCryptoUniverseRepository type rather than
                # a narrower Protocol (it is not touched by this milestone,
                # see the module comment block above) -- this wrapper only
                # ever needs its one ``observations()`` method, which it
                # genuinely satisfies; the cast documents that structural
                # substitution rather than widening the real class's own
                # constructor signature.
                cast(
                    PostgresCryptoUniverseRepository,
                    _MemoizedCryptoUniverseObservations(crypto_universe_repository),
                )
            ),
        )
        plan = plan_current_followup_selection(
            plan_service,
            as_of=datetime.now(tz=UTC),
            already_observed=already_observed_ids,
        )
        plan_summary = {
            "as_of": plan.as_of,
            "policy_version": " · ".join(plan.policy_versions),
            "policy_versions": list(plan.policy_versions),
            "policy_version_by_venue": [list(pair) for pair in plan.policy_version_by_venue],
            "considered_decisions": plan.considered_decisions,
            "considered_pairs": plan.considered_pairs,
            "state_counts": plan.state_counts,
            "state_counts_by_venue": plan.state_counts_by_venue,
        }
        scan_cursors = [
            cursor
            for policy_version in CURRENT_FOLLOWUP_POLICY_VERSIONS
            if (
                cursor := counterfactual_followup_repository.scan_cursor_diagnostics(
                    policy_version=policy_version
                )
            )
            is not None
        ]
        try:
            value = build_operator_followups(
                cast(list[dict[str, object]], _jsonable(observations)),
                cast(list[dict[str, object]], _jsonable(plan.entries)),
                decisions_by_id=cast(dict[str, dict[str, object]], _jsonable(decisions_by_id)),
                coverage=cast(
                    dict[str, object],
                    _jsonable(
                        counterfactual_followup_repository.coverage(
                            decisions=decisions, observations=observations
                        )
                    ),
                ),
                plan_summary=cast(dict[str, object], _jsonable(plan_summary)),
                plan_policy_versions=plan.policy_versions,
                policy_version=policy_version,
                baseline_kind=baseline_kind,
                horizon_kind=horizon_kind,
                venue=venue,
                status=status,
                limit=limit,
                offset=offset,
                latest_cycle_task=latest_effective_followup_cycle_task(),
                scan_cursors=cast(list[dict[str, object]], _jsonable(scan_cursors)),
                kalshi_request_budget=COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
                evidence_assessments_by_observation_id=cast(
                    dict[str, dict[str, object]],
                    _jsonable(evidence_assessments_by_observation_id),
                ),
                kalshi_resolution_census=cast(
                    dict[str, object], _jsonable(kalshi_resolution_repository.census())
                ),
                kalshi_baseline_quote_census=cast(
                    dict[str, object], _jsonable(kalshi_baseline_quote_repository.census())
                ),
            )
        except OperatorConsoleFilterError as error:
            raise _http_error(request, 400, str(error), "invalid follow-up filter") from error
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/options-research")
    def operator_console_options_research() -> dict[str, object]:
        """The options-forward research campaign: distinct-session progress
        toward the retained ``IV_RANK_MINIMUM_HISTORY`` target, genuine
        per-session history, and the latest session's per-underlying
        detail. ``resolve_options_capture_due_decision`` is the identical
        already-reviewed read-only resolution the existing
        ``/api/v1/options-intelligence/workspace`` view calls from this same
        GET-only surface (see ``_options_research_operations_summary``
        above); it reads only the retained exchange calendar and retained
        capability/checkpoint evidence and never performs a provider
        request or a capture."""

        from apex.adapters.options_research_capture import resolve_options_capture_due_decision

        now = datetime.now(tz=UTC)
        plan, window_policy, sessions, decision = resolve_options_capture_due_decision(
            quote_repository=options_quote_intelligence_repository,
            checkpoint_repository=options_research_operations_repository,
            as_of=now,
        )
        checkpoints = (
            options_research_operations_repository.checkpoints_for_plan(plan.plan_id)
            if plan is not None
            else ()
        )
        # A genuine miss: a calendar session whose own capture window has
        # already closed with no retained checkpoint for it -- read only
        # from the identical bounded calendar evidence
        # ``resolve_options_capture_due_decision`` already fetched above,
        # never a second calendar read.
        checkpoint_session_ids = {item.session_id for item in checkpoints}
        missed_session_ids = [
            session.session_date
            for session in sessions
            if window_policy.window_for_session(session)[1] < now
            and session.session_date not in checkpoint_session_ids
        ]
        latest_checkpoint = max(checkpoints, key=lambda item: item.created_at, default=None)
        latest_snapshots = (
            tuple(
                snapshot
                for snapshot_id in latest_checkpoint.fresh_snapshot_ids
                if (snapshot := options_quote_intelligence_repository.snapshot_by_id(snapshot_id))
                is not None
            )
            if latest_checkpoint is not None
            else ()
        )
        value = build_operator_options_research(
            cast(list[dict[str, object]], _jsonable(checkpoints)),
            target_session_count=IV_RANK_MINIMUM_HISTORY,
            latest_underlying_snapshots=cast(list[dict[str, object]], _jsonable(latest_snapshots)),
            capture_task=cast(dict[str, object], _jsonable(decision)),
            missed_session_ids=missed_session_ids,
        )
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/options-research/checkpoints/{checkpoint_id}")
    def operator_console_options_checkpoint_detail(
        checkpoint_id: str, request: Request
    ) -> dict[str, object]:
        checkpoint = options_research_operations_repository.checkpoint_by_id(checkpoint_id)
        if checkpoint is None:
            raise _http_error(
                request,
                404,
                "OPTIONS_FORWARD_CHECKPOINT_NOT_FOUND",
                "the exact retained checkpoint was not found",
                entity_type="OptionsForwardEvidenceCheckpoint",
                entity_id=checkpoint_id,
            )
        return _envelope(
            {"checkpoint": checkpoint, "authority": "NONE"}, capability="OPERATOR_READ_ONLY"
        )

    def _mandate_history(mandate_id: str | None, *, maximum: int = 10) -> list[Any]:
        chain: list[Any] = []
        seen: set[str] = set()
        cursor = investment_mandate_repository.mandate(mandate_id) if mandate_id else None
        while cursor is not None and cursor.mandate_id not in seen and len(chain) < maximum:
            chain.append(cursor)
            seen.add(cursor.mandate_id)
            cursor = (
                investment_mandate_repository.mandate(cursor.previous_mandate_id)
                if cursor.previous_mandate_id
                else None
            )
        return chain

    @app.get("/api/v1/operator-console/portfolio-mandate")
    def operator_console_portfolio_mandate() -> dict[str, object]:
        """Existing normalized PortfolioState and InvestmentMandate truth
        only. No valuation logic and no editing path exist here."""

        now = datetime.now(tz=UTC)
        snapshot = portfolio_state_repository.latest_portfolio_snapshot()
        balances = (
            portfolio_state_repository.portfolio_account_balances(snapshot.snapshot_id)
            if snapshot is not None
            else ()
        )
        positions = (
            portfolio_state_repository.portfolio_positions(snapshot.snapshot_id)
            if snapshot is not None
            else ()
        )
        mandate = investment_mandate_repository.active_mandate(
            DEFAULT_MANDATE_LINEAGE_ID, as_of=now
        )
        resolved_mandate = mandate or investment_mandate_repository.latest_mandate_version(
            DEFAULT_MANDATE_LINEAGE_ID
        )
        history = _mandate_history(resolved_mandate.mandate_id if resolved_mandate else None)
        evaluations = investment_mandate_repository.all_evaluations()
        value = build_operator_portfolio_mandate(
            portfolio_snapshot=cast(dict[str, object] | None, _jsonable(snapshot)),
            account_balances=cast(list[dict[str, object]], _jsonable(balances)),
            positions=cast(list[dict[str, object]], _jsonable(positions)),
            mandate=cast(dict[str, object] | None, _jsonable(mandate)),
            mandate_lineage_versions=cast(list[dict[str, object]], _jsonable(history)),
            latest_evaluations=cast(list[dict[str, object]], _jsonable(evaluations)),
        )
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/operator-console/portfolio-mandate/evaluations/{evaluation_id}")
    def operator_console_mandate_evaluation_detail(
        evaluation_id: str, request: Request
    ) -> dict[str, object]:
        evaluation = investment_mandate_repository.evaluation(evaluation_id)
        if evaluation is None:
            raise _http_error(
                request,
                404,
                "MANDATE_EVALUATION_NOT_FOUND",
                "the exact retained mandate evaluation was not found",
                entity_type="MandateEvaluation",
                entity_id=evaluation_id,
            )
        return _envelope(
            {"evaluation": evaluation, "authority": "NONE"}, capability="OPERATOR_READ_ONLY"
        )

    @app.get("/api/v1/operator-console/operations")
    def operator_console_operations(
        request: Request,
        task_type: str = "ALL",
        status: str = "ALL",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        """A bounded, newest-first timeline of already-retained
        operating-cycle task executions across the most recent cycles,
        highlighting ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE`` and
        ``COUNTERFACTUAL_FOLLOWUP_REFRESH``."""

        _page(request, limit, offset)
        cycles = sorted(operating_cycle_repository.cycles(), key=lambda item: item.created_at)
        recent_cycles = cycles[-15:]
        task_rows: list[dict[str, object]] = []
        for cycle in recent_cycles:
            task_rows.extend(
                cast(
                    list[dict[str, object]],
                    _jsonable(operating_cycle_repository.task_results(cycle.cycle_id)),
                )
            )
        latest_cycle = recent_cycles[-1] if recent_cycles else None
        latest_due = (
            operating_cycle_repository.due_work(latest_cycle.cycle_id)
            if latest_cycle is not None
            else None
        )
        latest_due_tasks = (
            cast(list[dict[str, object]], _jsonable(latest_due.tasks))
            if latest_due is not None
            else []
        )
        try:
            value = build_operator_operations(
                task_rows,
                latest_due_tasks=latest_due_tasks,
                latest_cycle_id=latest_cycle.cycle_id if latest_cycle is not None else None,
                latest_cycle_as_of=(
                    cast(str, _jsonable(latest_cycle.as_of)) if latest_cycle is not None else None
                ),
                task_type=task_type,
                status=status,
                limit=limit,
                offset=offset,
            )
        except OperatorConsoleFilterError as error:
            raise _http_error(request, 400, str(error), "invalid operations filter") from error
        return _envelope(value, capability="OPERATOR_READ_ONLY")

    @app.get("/api/v1/live-preflight/intents/{intent_id}")
    def live_preflight_intent_read(intent_id: str, request: Request) -> dict[str, object]:
        value = live_preflight_repository.intent(intent_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "LIVE_INTENT_NOT_FOUND",
                "the exact immutable LIVE intent was not found",
                entity_type="LiveIntentProposal",
                entity_id=intent_id,
            )
        return _envelope(
            {"intent": value, "authority": "NONE", "live_authority": "NONE"},
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    @app.get("/api/v1/live-preflight/internal/{preflight_id}")
    def live_internal_preflight_read(preflight_id: str, request: Request) -> dict[str, object]:
        value = live_preflight_repository.preflight(preflight_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "LIVE_PREFLIGHT_NOT_FOUND",
                "the exact immutable internal preflight was not found",
                entity_type="InternalPreflight",
                entity_id=preflight_id,
            )
        return _envelope(
            {"preflight": value, "authority": "NONE", "live_authority": "NONE"},
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    @app.get("/api/v1/live-preflight/provider-preview-approvals/{approval_id}")
    def provider_preview_approval_read(approval_id: str, request: Request) -> dict[str, object]:
        value = live_preflight_repository.preview_approval(approval_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "PROVIDER_PREVIEW_APPROVAL_NOT_FOUND",
                "the exact immutable provider preview approval was not found",
                entity_type="ProviderPreviewApproval",
                entity_id=approval_id,
            )
        return _envelope(
            {"approval": value, "authority": "NONE", "live_authority": "NONE"},
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    @app.get("/api/v1/live-preflight/provider-preview-attempts/{attempt_id}")
    def provider_preview_attempt_read(attempt_id: str, request: Request) -> dict[str, object]:
        value = live_preflight_repository.preview_attempt(attempt_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "PROVIDER_PREVIEW_ATTEMPT_NOT_FOUND",
                "the exact immutable provider preview attempt was not found",
                entity_type="ProviderPreviewAttempt",
                entity_id=attempt_id,
            )
        return _envelope(
            {"attempt": value, "authority": "NONE", "live_authority": "NONE"},
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    @app.get("/api/v1/live-preflight/provider-previews/{capture_id}")
    def provider_preview_capture_read(capture_id: str, request: Request) -> dict[str, object]:
        value = live_preflight_repository.preview_capture(capture_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "PROVIDER_PREVIEW_CAPTURE_NOT_FOUND",
                "the exact redacted provider preview capture was not found",
                entity_type="ProviderPreviewCapture",
                entity_id=capture_id,
            )
        return _envelope(
            {"capture": value, "authority": "NONE", "live_authority": "NONE"},
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    @app.get("/api/v1/live-preflight/assessments/{assessment_id}")
    def live_preflight_assessment_read(assessment_id: str, request: Request) -> dict[str, object]:
        value = live_preflight_repository.assessment(assessment_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "LIVE_PREFLIGHT_ASSESSMENT_NOT_FOUND",
                "the exact immutable preflight assessment was not found",
                entity_type="LivePreflightAssessment",
                entity_id=assessment_id,
            )
        return _envelope(
            {"assessment": value, "authority": "NONE", "live_authority": "NONE"},
            capability="LIVE_PREFLIGHT_READ_ONLY",
        )

    @app.get("/api/v1/operational-reliability/workspace")
    def operational_reliability_workspace_read() -> dict[str, object]:
        return _envelope(
            operational_workspace(operational_reliability_repository),
            capability="OPERATIONAL_RELIABILITY_READ_ONLY",
        )

    @app.post(
        "/api/v1/live-readiness/coinbase/read-approvals",
        dependencies=[Depends(_mutation_guard)],
    )
    def approve_coinbase_private_read(
        body: CoinbaseReadApprovalRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            value = ApproveCoinbaseReadOnlyInspection(
                coinbase_read_only_repository,
                live_readiness_repository,
            ).execute(
                approval_id=body.approval_id,
                credential_reference_id=body.credential_reference_id,
                account_purpose=body.account_purpose,
                allowed_endpoint_classes=tuple(
                    CoinbaseEndpointClass(item) for item in body.allowed_endpoint_classes
                ),
                maximum_request_count=body.maximum_request_count,
                timeout_seconds=body.timeout_seconds,
                approved_at=datetime.now(tz=UTC),
                expires_at=body.expires_at,
                actor=body.actor,
                reason=body.reason,
                idempotency_key=idempotency_key,
                confirmation=body.confirmation,
            )
            return _envelope(value, capability="COINBASE_PRIVATE_READ_APPROVAL_ONLY")
        except (CoinbaseReadOnlyError, ValueError) as error:
            raise _http_error(
                request,
                422,
                "COINBASE_READ_APPROVAL_REJECTED",
                str(error),
            ) from error

    @app.post(
        "/api/v1/live-readiness/coinbase/read-inspections",
        dependencies=[Depends(_mutation_guard)],
    )
    def inspect_coinbase_private_read(
        body: CoinbaseReadOnlyInspectionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        if idempotency_key != body.attempt_id:
            raise _http_error(
                request,
                422,
                "COINBASE_READ_INSPECTION_EXACT_IDEMPOTENCY_REQUIRED",
                "the attempt identity must exactly match the idempotency key",
            )
        controls = live_readiness_repository.control_events(body.control_id)
        control_state = ExecutionControlState.LIVE_BLOCKED if not controls else controls[-1].state
        try:
            result = InspectCoinbaseReadOnlyCapability(
                coinbase_read_only_repository,
                live_readiness_repository,
                UnavailableCoinbaseCredentialResolver(),
                NetworkDisabledCoinbasePrivateTransport(),
            ).execute(
                attempt_id=body.attempt_id,
                capture_id=body.capture_id,
                readiness_snapshot_id=body.readiness_snapshot_id,
                approval_id=body.approval_id,
                credential_reference_id=body.credential_reference_id,
                account_purpose=body.account_purpose,
                product_ids=body.product_ids,
                inspected_at=datetime.now(tz=UTC),
                execution_control=control_state,
            )
            return _envelope(result, capability="COINBASE_PRIVATE_READ_ONLY")
        except (CoinbaseReadOnlyError, ValueError) as error:
            raise _http_error(
                request,
                422,
                "COINBASE_READ_INSPECTION_REJECTED",
                str(error),
            ) from error

    @app.get("/api/v1/live-readiness/coinbase/read-only-workspace")
    def coinbase_private_read_workspace() -> dict[str, object]:
        return _envelope(
            coinbase_read_only_workspace(coinbase_read_only_repository),
            capability="COINBASE_PRIVATE_READ_ONLY",
        )

    @app.get("/api/v1/live-readiness/coinbase/read-approvals/{approval_id}")
    def coinbase_private_read_approval(approval_id: str, request: Request) -> dict[str, object]:
        value = coinbase_read_only_repository.approval(approval_id)
        if value is None:
            raise _http_error(
                request, 404, "COINBASE_READ_APPROVAL_NOT_FOUND", "approval not found"
            )
        return _envelope(value, capability="COINBASE_PRIVATE_READ_ONLY")

    @app.get("/api/v1/live-readiness/coinbase/captures/{capture_id}")
    def coinbase_private_read_capture(capture_id: str, request: Request) -> dict[str, object]:
        value = coinbase_read_only_repository.capture(capture_id)
        if value is None:
            raise _http_error(
                request, 404, "COINBASE_CAPABILITY_CAPTURE_NOT_FOUND", "capture not found"
            )
        return _envelope(value, capability="COINBASE_PRIVATE_READ_ONLY")

    @app.post(
        "/api/v1/live-readiness/kalshi/read-approvals",
        dependencies=[Depends(_mutation_guard)],
    )
    def approve_kalshi_private_read(
        body: KalshiReadApprovalRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            value = ApproveKalshiReadOnlyInspection(
                kalshi_read_only_repository,
                live_readiness_repository,
            ).execute(
                approval_id=body.approval_id,
                credential_reference_id=body.credential_reference_id,
                account_purpose=body.account_purpose,
                allowed_endpoint_classes=tuple(
                    KalshiEndpointClass(item) for item in body.allowed_endpoint_classes
                ),
                maximum_request_count=body.maximum_request_count,
                timeout_seconds=body.timeout_seconds,
                approved_at=datetime.now(tz=UTC),
                expires_at=body.expires_at,
                actor=body.actor,
                reason=body.reason,
                idempotency_key=idempotency_key,
                confirmation=body.confirmation,
            )
            return _envelope(value, capability="KALSHI_PRIVATE_READ_APPROVAL_ONLY")
        except (KalshiReadOnlyError, ValueError) as error:
            raise _http_error(request, 422, "KALSHI_READ_APPROVAL_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/live-readiness/kalshi/read-inspections",
        dependencies=[Depends(_mutation_guard)],
    )
    def inspect_kalshi_private_read(
        body: KalshiReadOnlyInspectionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        if idempotency_key != body.attempt_id:
            raise _http_error(
                request,
                422,
                "KALSHI_READ_INSPECTION_EXACT_IDEMPOTENCY_REQUIRED",
                "the attempt identity must exactly match the idempotency key",
            )
        controls = live_readiness_repository.control_events(body.control_id)
        control_state = ExecutionControlState.LIVE_BLOCKED if not controls else controls[-1].state
        try:
            result = InspectKalshiReadOnlyCapability(
                kalshi_read_only_repository,
                live_readiness_repository,
                UnavailableKalshiCredentialResolver(),
                NetworkDisabledKalshiPrivateTransport(),
            ).execute(
                attempt_id=body.attempt_id,
                capture_id=body.capture_id,
                readiness_snapshot_id=body.readiness_snapshot_id,
                approval_id=body.approval_id,
                credential_reference_id=body.credential_reference_id,
                account_purpose=body.account_purpose,
                inspected_at=datetime.now(tz=UTC),
                execution_control=control_state,
            )
            return _envelope(result, capability="KALSHI_PRIVATE_READ_ONLY")
        except (KalshiReadOnlyError, ValueError) as error:
            raise _http_error(
                request, 422, "KALSHI_READ_INSPECTION_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/live-readiness/kalshi/read-only-workspace")
    def kalshi_private_read_workspace() -> dict[str, object]:
        return _envelope(
            kalshi_read_only_workspace(kalshi_read_only_repository),
            capability="KALSHI_PRIVATE_READ_ONLY",
        )

    @app.get("/api/v1/live-readiness/kalshi/read-approvals/{approval_id}")
    def kalshi_private_read_approval(approval_id: str, request: Request) -> dict[str, object]:
        value = kalshi_read_only_repository.approval(approval_id)
        if value is None:
            raise _http_error(request, 404, "KALSHI_READ_APPROVAL_NOT_FOUND", "approval not found")
        return _envelope(value, capability="KALSHI_PRIVATE_READ_ONLY")

    @app.get("/api/v1/live-readiness/kalshi/captures/{capture_id}")
    def kalshi_private_read_capture(capture_id: str, request: Request) -> dict[str, object]:
        value = kalshi_read_only_repository.capture(capture_id)
        if value is None:
            raise _http_error(
                request, 404, "KALSHI_CAPABILITY_CAPTURE_NOT_FOUND", "capture not found"
            )
        return _envelope(value, capability="KALSHI_PRIVATE_READ_ONLY")

    @app.post(
        "/api/v1/live-readiness/credential-references",
        dependencies=[Depends(_mutation_guard)],
    )
    def register_live_readiness_credential_reference(
        body: CredentialReferenceRegistrationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            value = RegisterCredentialReference(live_readiness_repository).execute(
                reference_id=body.reference_id,
                version=body.version,
                provider=Provider(body.provider),
                account_environment=AccountEnvironment(body.account_environment),
                account_purpose=body.account_purpose,
                required_permissions=tuple(
                    PermissionClass(permission) for permission in body.required_permissions
                ),
                secret_backend=SecretBackend(body.secret_backend),
                backend_reference_id=body.backend_reference_id,
                actor=body.actor,
                reason=body.reason,
                confirmation=body.confirmation,
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
                previous_reference_id=body.previous_reference_id,
            )
            safe = cast(dict[str, object], _jsonable(value))
            safe.pop("backend_reference_id", None)
            return _envelope(safe, capability="LIVE_READINESS_CONTROL")
        except (LiveReadinessError, ValueError) as error:
            raise _http_error(request, 422, "CREDENTIAL_REFERENCE_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/live-readiness/capabilities",
        dependencies=[Depends(_mutation_guard)],
    )
    def inspect_live_readiness_capability(
        body: ProviderCapabilityInspectionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            provider = Provider(body.provider)
            value = InspectProviderCapability(
                live_readiness_repository,
                CredentialsAbsentCapabilityInspector(provider),
            ).execute(
                snapshot_id=body.snapshot_id,
                account_scope_id=body.account_scope_id,
                account_environment=AccountEnvironment(body.account_environment),
                credential_reference_id=body.credential_reference_id,
                inspected_at=datetime.now(tz=UTC),
                request_limit=body.request_limit,
                actor=body.actor,
                reason=body.reason,
                confirmation=body.confirmation,
                idempotency_key=idempotency_key,
            )
            return _envelope(value, capability="LIVE_READINESS_CONTROL")
        except (LiveReadinessError, ValueError) as error:
            raise _http_error(
                request, 422, "PROVIDER_CAPABILITY_INSPECTION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/live-readiness/execution-controls/{control_id}",
        dependencies=[Depends(_mutation_guard)],
    )
    def set_live_readiness_execution_control(
        control_id: str,
        body: ExecutionControlTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            value = SetExecutionControl(live_readiness_repository).execute(
                event_id=body.event_id,
                control_id=control_id,
                state=ExecutionControlState(body.state),
                actor=body.actor,
                reason=body.reason,
                confirmation=body.confirmation,
                idempotency_key=idempotency_key,
                effective_at=datetime.now(tz=UTC),
            )
            return _envelope(value, capability="LIVE_READINESS_CONTROL")
        except (LiveReadinessError, ValueError) as error:
            raise _http_error(
                request, 422, "EXECUTION_CONTROL_TRANSITION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/live-readiness/assessments",
        dependencies=[Depends(_mutation_guard)],
    )
    def materialize_live_readiness_assessment(
        body: LiveReadinessMaterializationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        expected = f"MATERIALIZE_LIVE_READINESS:{body.assessment_id}"
        if body.confirmation != expected or not idempotency_key.strip():
            raise _http_error(
                request,
                422,
                "LIVE_READINESS_EXACT_CONFIRMATION_REQUIRED",
                "the exact readiness materialization confirmation is required",
            )
        try:
            provider = Provider(body.provider)
            now = datetime.now(tz=UTC)
            controls = live_readiness_repository.control_events(body.control_id)
            if not controls:
                raise LiveReadinessError("EXECUTION_CONTROL_NOT_FOUND")
            policy = live_readiness_repository.risk_policy(body.policy_id)
            if policy is None:
                if body.policy_id != "LIVE_RISK_POLICY_V1":
                    raise LiveReadinessError("LIVE_RISK_POLICY_NOT_FOUND")
                policy = LiveRiskPolicy.v1(created_at=now)
            value = MaterializeLiveReadiness(live_readiness_repository).execute(
                assessment_id=body.assessment_id,
                decision_id=body.decision_id,
                policy=policy,
                inputs=LiveReadinessInputs(
                    venue=provider,
                    account_scope_id=body.account_scope_id,
                    capability=live_readiness_repository.latest_capability(
                        provider, body.account_scope_id
                    ),
                    strategy_or_method_id=None,
                    paper_sample_count=0,
                    fill_count=0,
                    settlement_count=0,
                    total_live_capital=Decimal("0"),
                    retained_cash_ratio=Decimal("1"),
                    per_opportunity_exposure=Decimal("0"),
                    per_strategy_exposure=Decimal("0"),
                    per_instrument_exposure=Decimal("0"),
                    per_venue_exposure=Decimal("0"),
                    open_order_count=0,
                    open_position_count=0,
                    daily_realized_loss=Decimal("0"),
                    daily_modeled_loss=Decimal("0"),
                    drawdown_ratio=Decimal("0"),
                    strategy_health_state="INSUFFICIENT_SAMPLE",
                    reconciliation_quality="UNAVAILABLE",
                    unresolved_reconciliation_severity=1,
                    operating_cycle_health="UNAVAILABLE",
                    provider_health="UNAVAILABLE",
                    backup=live_readiness_repository.latest_backup_readiness(),
                    control=controls[-1],
                    qualifying_opportunity=False,
                    explicit_operator_review=True,
                    assessed_at=now,
                ),
                actor=body.actor,
            )
            return _envelope(value, capability="LIVE_READINESS_CONTROL")
        except (LiveReadinessError, ValueError) as error:
            raise _http_error(
                request, 422, "LIVE_READINESS_MATERIALIZATION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/live-readiness/exports",
        dependencies=[Depends(_mutation_guard)],
    )
    def export_live_readiness_evidence(
        body: LiveReadinessExportRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        expected = f"EXPORT_LIVE_READINESS:{body.export_id}"
        if body.confirmation != expected or not idempotency_key.strip():
            raise _http_error(
                request,
                422,
                "LIVE_READINESS_EXPORT_EXACT_CONFIRMATION_REQUIRED",
                "the exact readiness export confirmation is required",
            )
        try:
            assessment = live_readiness_repository.assessment(body.assessment_id)
            if assessment is None:
                raise LiveReadinessError("LIVE_READINESS_ASSESSMENT_NOT_FOUND")
            backup_count = int(live_readiness_repository.latest_backup_readiness() is not None)
            value = ExportLiveReadinessEvidence(live_readiness_repository).execute(
                export_id=body.export_id,
                assessment_id=body.assessment_id,
                categories=(
                    ("PROVIDER_CAPABILITIES", int(assessment.capability_snapshot_id is not None)),
                    ("READINESS_ASSESSMENTS", 1),
                    ("RISK_DECISIONS", 1),
                    ("EXECUTION_CONTROLS", 1),
                    ("ORDERS_AND_FILLS", 0),
                    ("POSITIONS_AND_SETTLEMENTS", 0),
                    ("RECONCILIATION", 0),
                    ("JOURNALS", 0),
                    ("AUTHORITY_HISTORY", 0),
                    ("BACKUP_READINESS", backup_count),
                ),
                evidence_ids=(assessment.assessment_id,),
                evidence_hashes=(assessment.canonical_hash,),
                paper_records=0,
                generated_at=datetime.now(tz=UTC),
            )
            return _envelope(value, capability="LIVE_READINESS_CONTROL")
        except (LiveReadinessError, ValueError) as error:
            raise _http_error(request, 422, "LIVE_READINESS_EXPORT_REJECTED", str(error)) from error

    @app.get("/api/v1/live-readiness/workspace")
    def live_readiness_workspace_read() -> dict[str, object]:
        return _envelope(
            live_readiness_workspace(live_readiness_repository),
            capability="LIVE_READINESS_READ_ONLY",
        )

    @app.get("/api/v1/live-readiness/credential-references/{reference_id}")
    def live_readiness_credential_read(reference_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.credential_reference(reference_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CREDENTIAL_REFERENCE_NOT_FOUND",
                "the exact sanitized credential reference was not found",
            )
        safe = _jsonable(value)
        safe.pop("backend_reference_id", None)
        return _envelope(safe, capability="LIVE_READINESS_READ_ONLY")

    @app.get("/api/v1/live-readiness/capabilities/{snapshot_id}")
    def live_readiness_capability_read(snapshot_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.capability(snapshot_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "PROVIDER_CAPABILITY_NOT_FOUND",
                "the exact provider capability snapshot was not found",
            )
        return _envelope(value, capability="LIVE_READINESS_READ_ONLY")

    @app.get("/api/v1/live-readiness/risk-policies/{policy_id}")
    def live_readiness_policy_read(policy_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.risk_policy(policy_id)
        if value is None:
            raise _http_error(request, 404, "LIVE_RISK_POLICY_NOT_FOUND", "policy not found")
        return _envelope(value, capability="LIVE_READINESS_READ_ONLY")

    @app.get("/api/v1/live-readiness/execution-controls/{control_id}")
    def live_readiness_control_read(control_id: str) -> dict[str, object]:
        values = live_readiness_repository.control_events(control_id)
        return _envelope(
            {"events": values, "current": None if not values else values[-1]},
            capability="LIVE_READINESS_READ_ONLY",
        )

    @app.get("/api/v1/live-readiness/backups/{record_id}")
    def live_readiness_backup_read(record_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.backup_readiness(record_id)
        if value is None:
            raise _http_error(request, 404, "BACKUP_READINESS_NOT_FOUND", "record not found")
        return _envelope(value, capability="LIVE_READINESS_READ_ONLY")

    @app.get("/api/v1/live-readiness/assessments/{assessment_id}")
    def live_readiness_assessment_read(assessment_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.assessment(assessment_id)
        if value is None:
            raise _http_error(request, 404, "LIVE_READINESS_NOT_FOUND", "assessment not found")
        return _envelope(value, capability="LIVE_READINESS_READ_ONLY")

    @app.get("/api/v1/live-readiness/assessments/{assessment_id}/blockers")
    def live_readiness_blockers_read(assessment_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.assessment(assessment_id)
        if value is None:
            raise _http_error(request, 404, "LIVE_READINESS_NOT_FOUND", "assessment not found")
        return _envelope(
            {
                "assessment_id": value.assessment_id,
                "recommendation": value.recommendation,
                "blockers": value.unresolved_blockers,
                "authority": "NONE",
                "live_authority": "NONE",
            },
            capability="LIVE_READINESS_READ_ONLY",
        )

    @app.get("/api/v1/live-readiness/exports/{export_id}")
    def live_readiness_export_read(export_id: str, request: Request) -> dict[str, object]:
        value = live_readiness_repository.export(export_id)
        if value is None:
            raise _http_error(request, 404, "LIVE_READINESS_EXPORT_NOT_FOUND", "export not found")
        return _envelope(value, capability="LIVE_READINESS_READ_ONLY")

    @app.get("/api/v1/cross-venue-allocation/workspace")
    def cross_venue_allocation_workspace() -> dict[str, object]:
        """Read-only hypothetical allocation and PAPER trial proposal evidence."""
        return _securities_envelope(cross_venue_allocation_repository.workspace())

    @app.get("/api/v1/performance-attribution/workspace")
    def performance_attribution_workspace() -> dict[str, object]:
        """Read-only shared attribution, health, and PAPER summary evidence."""
        return _envelope(
            performance_attribution_repository.workspace(),
            capability="PAPER_ATTRIBUTION_READ_ONLY",
        )

    @app.get("/api/v1/performance-attribution/attributions/{attribution_id}")
    def performance_attribution_read(attribution_id: str, request: Request) -> dict[str, object]:
        value = performance_attribution_repository.attribution(attribution_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "PERFORMANCE_ATTRIBUTION_NOT_FOUND",
                "the exact immutable PAPER attribution was not found",
                entity_type="SharedPerformanceAttribution",
                entity_id=attribution_id,
            )
        return _envelope(
            {
                "attribution": value,
                "components": performance_attribution_repository.components(attribution_id),
                "authority": "NONE",
                "live_authority": "NONE",
            },
            capability="PAPER_ATTRIBUTION_READ_ONLY",
        )

    @app.get("/api/v1/performance-attribution/health/{health_id}")
    def strategy_health_read(health_id: str, request: Request) -> dict[str, object]:
        value = performance_attribution_repository.health(health_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "STRATEGY_HEALTH_NOT_FOUND",
                "the exact immutable strategy-health assessment was not found",
                entity_type="StrategyHealthAssessment",
                entity_id=health_id,
            )
        return _envelope(
            {"strategy_health": value, "authority": "NONE", "live_authority": "NONE"},
            capability="PAPER_ATTRIBUTION_READ_ONLY",
        )

    @app.get("/api/v1/cross-venue-allocation/decisions/{decision_id}")
    def cross_venue_allocation_read(decision_id: str, request: Request) -> dict[str, object]:
        decision = cross_venue_allocation_repository.decision(decision_id)
        if decision is None:
            raise _http_error(
                request,
                404,
                "CROSS_VENUE_ALLOCATION_NOT_FOUND",
                "the exact immutable cross-venue allocation decision was not found",
                entity_type="CrossVenueAllocationDecision",
                entity_id=decision_id,
            )
        return _securities_envelope(
            {
                "decision": decision,
                "lines": cross_venue_allocation_repository.lines(decision_id),
                "explanations": cross_venue_allocation_repository.explanations(decision_id),
                "paper_trial_proposals": cross_venue_allocation_repository.proposals(decision_id),
                "paper_authorization_created": False,
                "orders_created": 0,
                "authority": "NONE",
                "live_authority": "NONE",
            }
        )

    @app.get("/api/v1/cross-venue-allocation/proposals/{proposal_id}")
    def cross_venue_paper_trial_proposal_read(
        proposal_id: str, request: Request
    ) -> dict[str, object]:
        proposal = cross_venue_allocation_repository.proposal(proposal_id)
        if proposal is None:
            raise _http_error(
                request,
                404,
                "CROSS_VENUE_PAPER_TRIAL_PROPOSAL_NOT_FOUND",
                "the exact immutable PAPER trial proposal was not found",
                entity_type="PaperTrialProposal",
                entity_id=proposal_id,
            )
        return _securities_envelope(
            {
                "paper_trial_proposal": proposal,
                "paper_authorization_created": False,
                "order_created": False,
                "authority": "NONE",
                "live_authority": "NONE",
            }
        )

    @app.get("/api/v1/kalshi-operating/workspace/{account_id}")
    def kalshi_operating_workspace(account_id: str) -> dict[str, object]:
        capability = kalshi_operating_repository.latest_capability()
        refresh = kalshi_operating_repository.latest_refresh()
        value = kalshi_workspace(
            repository=kalshi_operating_repository,
            account_id=account_id,
            capability_id=None if capability is None else capability.capability_id,
            refresh_id=None if refresh is None else refresh.refresh_id,
        )
        value["shared_opportunity"] = shared_opportunity_repository.workspace()
        value["total_market_context"] = total_market_context_repository.workspace()
        value["operating_cycle"] = operating_cycle_repository.workspace()
        return _kalshi_paper_envelope(value)

    @app.get("/api/v1/kalshi-operating/capabilities/{capability_id}")
    def kalshi_capability_read(capability_id: str, request: Request) -> dict[str, object]:
        value = kalshi_operating_repository.capability(capability_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "KALSHI_CAPABILITY_NOT_FOUND",
                "the exact retained Kalshi capability record was not found",
                entity_type="KalshiCapability",
                entity_id=capability_id,
            )
        return _kalshi_paper_envelope({"provider_capability": value})

    @app.get("/api/v1/kalshi-operating/refreshes/{refresh_id}")
    def kalshi_refresh_read(refresh_id: str, request: Request) -> dict[str, object]:
        value = kalshi_operating_repository.refresh(refresh_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "KALSHI_REFRESH_NOT_FOUND",
                "the exact retained Kalshi current refresh was not found",
                entity_type="KalshiRefreshRecord",
                entity_id=refresh_id,
            )
        return _kalshi_paper_envelope(
            {
                "refresh": value,
                "opportunities": (
                    kalshi_operating_repository.opportunities_for_refresh(refresh_id)
                ),
            }
        )

    @app.get("/api/v1/kalshi-operating/refreshes/{refresh_id}/opportunities/{opportunity_id}")
    def kalshi_opportunity_read(
        refresh_id: str, opportunity_id: str, request: Request
    ) -> dict[str, object]:
        value = next(
            (
                item
                for item in kalshi_operating_repository.opportunities_for_refresh(refresh_id)
                if item.opportunity_id == opportunity_id
            ),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "KALSHI_OPPORTUNITY_NOT_FOUND",
                "the exact current Kalshi Opportunity was not found",
                entity_type="KalshiCurrentOpportunity",
                entity_id=opportunity_id,
            )
        return _kalshi_paper_envelope({"opportunity": value})

    def kalshi_paper_state(account_id: str, request: Request) -> Any:
        value = kalshi_operating_repository.state(account_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "KALSHI_PAPER_ACCOUNT_NOT_FOUND",
                "the exact internal Kalshi PAPER account was not found",
                entity_type="KalshiPaperAccount",
                entity_id=account_id,
            )
        return value

    @app.get("/api/v1/kalshi-paper/accounts/{account_id}")
    def kalshi_paper_account_read(account_id: str, request: Request) -> dict[str, object]:
        value = kalshi_paper_state(account_id, request)
        return _kalshi_paper_envelope(
            {
                "account": value.account,
                "cash": value.cash,
                "reserved_cash": value.reserved_cash,
                "available_cash": value.available_cash,
                "modeled_fees": value.modeled_fees,
                "realized_paper_pnl": value.realized_paper_pnl,
                "balances_are_genuine": False,
                "paper_authority": "PAPER_ONLY",
                "live_authority": "NONE",
            }
        )

    @app.get("/api/v1/kalshi-paper/accounts/{account_id}/{entity_type}/{entity_id}")
    def kalshi_paper_entity_read(
        account_id: str,
        entity_type: str,
        entity_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = kalshi_paper_state(account_id, request)
        collection_name, identity_name = {
            "authorizations": ("authorizations", "authorization_id"),
            "previews": ("previews", "preview_id"),
            "orders": ("orders", "order_id"),
            "fills": ("fills", "fill_id"),
            "positions": ("positions", "position_id"),
            "settlements": ("settlements", "settlement_id"),
            "reconciliations": ("reconciliations", "reconciliation_id"),
        }.get(entity_type, ("", ""))
        if not collection_name:
            raise _http_error(
                request,
                404,
                "KALSHI_PAPER_ENTITY_TYPE_NOT_FOUND",
                "the requested Kalshi PAPER entity type is unavailable",
                entity_type="KalshiPaperEntityType",
                entity_id=entity_type,
            )
        value = next(
            (
                item
                for item in getattr(state, collection_name)
                if getattr(item, identity_name) == entity_id
            ),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "KALSHI_PAPER_ENTITY_NOT_FOUND",
                "the exact internal Kalshi PAPER entity was not found",
                entity_type=entity_type,
                entity_id=entity_id,
            )
        return _kalshi_paper_envelope({entity_type.rstrip("s"): value})

    @app.get("/api/v1/crypto-paper/workspace/{account_id}")
    def crypto_paper_workspace(account_id: str) -> dict[str, object]:
        state = crypto_paper_repository.state(account_id)
        workspace = paper_workspace(state)
        workspace["operating_cycle_seam"] = InspectCryptoPaperOperatingSeam(
            crypto_paper_repository
        ).execute(account_id=account_id)
        return _crypto_paper_envelope(workspace)

    @app.get("/api/v1/crypto-paper/capabilities/{capability_id}")
    def crypto_paper_capability_read(
        capability_id: str,
        request: Request,
    ) -> dict[str, object]:
        value = crypto_paper_repository.capability(capability_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_CAPABILITY_NOT_FOUND",
                "the exact retained Crypto PAPER capability observation was not found",
                entity_type="CryptoPaperCapability",
                entity_id=capability_id,
            )
        return _crypto_paper_envelope({"provider_capability": value})

    @app.get("/api/v1/crypto-paper/accounts/{account_id}")
    def crypto_paper_account_read(
        account_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_repository.state(account_id)
        if state is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_ACCOUNT_NOT_FOUND",
                "the exact Apex-owned Crypto PAPER account was not found",
                entity_type="CryptoPaperAccount",
                entity_id=account_id,
            )
        return _crypto_paper_envelope(
            {
                "account": state.account,
                "account_state": state.account_state,
                "balances_are_genuine": False,
                "provider_requests": 0,
                "paper_authority": "PAPER_ONLY",
                "live_authority": "NONE",
            }
        )

    def crypto_paper_state(account_id: str, request: Request) -> Any:
        state = crypto_paper_repository.state(account_id)
        if state is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_ACCOUNT_NOT_FOUND",
                "the exact Apex-owned Crypto PAPER account was not found",
                entity_type="CryptoPaperAccount",
                entity_id=account_id,
            )
        return state

    @app.get("/api/v1/crypto-paper/accounts/{account_id}/authorizations/{authorization_id}")
    def crypto_paper_authorization_read(
        account_id: str,
        authorization_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_state(account_id, request)
        value = next(
            (item for item in state.authorizations if item.authorization_id == authorization_id),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_AUTHORIZATION_NOT_FOUND",
                "the exact Crypto PAPER authorization was not found",
                entity_type="CryptoPaperAuthorization",
                entity_id=authorization_id,
            )
        return _crypto_paper_envelope({"authorization": value})

    @app.get("/api/v1/crypto-paper/accounts/{account_id}/previews/{preview_id}")
    def crypto_paper_preview_read(
        account_id: str,
        preview_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_state(account_id, request)
        value = next(
            (item for item in state.previews if item.preview_id == preview_id),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_PREVIEW_NOT_FOUND",
                "the exact immutable Crypto PAPER preview was not found",
                entity_type="CryptoPaperPreview",
                entity_id=preview_id,
            )
        return _crypto_paper_envelope({"preview": value})

    @app.get("/api/v1/crypto-paper/accounts/{account_id}/orders/{order_id}")
    def crypto_paper_order_read(
        account_id: str,
        order_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_state(account_id, request)
        value = next((item for item in state.orders if item.order_id == order_id), None)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_ORDER_NOT_FOUND",
                "the exact internal Crypto PAPER order was not found",
                entity_type="CryptoPaperOrder",
                entity_id=order_id,
            )
        return _crypto_paper_envelope(
            {
                "order": value,
                "events": tuple(item for item in state.events if item.order_id == order_id),
                "reservation": next(
                    (item for item in state.reservations if item.order_id == order_id),
                    None,
                ),
            }
        )

    @app.get("/api/v1/crypto-paper/accounts/{account_id}/fills/{fill_id}")
    def crypto_paper_fill_read(
        account_id: str,
        fill_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_state(account_id, request)
        value = next((item for item in state.fills if item.fill_id == fill_id), None)
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_FILL_NOT_FOUND",
                "the exact simulated Crypto PAPER fill was not found",
                entity_type="CryptoPaperFill",
                entity_id=fill_id,
            )
        return _crypto_paper_envelope({"fill": value})

    @app.get("/api/v1/crypto-paper/accounts/{account_id}/positions/{position_id}")
    def crypto_paper_position_read(
        account_id: str,
        position_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_state(account_id, request)
        value = next(
            (item for item in state.positions if item.position_id == position_id),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_POSITION_NOT_FOUND",
                "the exact simulated Crypto PAPER position was not found",
                entity_type="CryptoPaperPosition",
                entity_id=position_id,
            )
        return _crypto_paper_envelope({"position": value})

    @app.get("/api/v1/crypto-paper/accounts/{account_id}/reconciliations/{reconciliation_id}")
    def crypto_paper_reconciliation_read(
        account_id: str,
        reconciliation_id: str,
        request: Request,
    ) -> dict[str, object]:
        state = crypto_paper_state(account_id, request)
        value = next(
            (item for item in state.reconciliations if item.reconciliation_id == reconciliation_id),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "CRYPTO_PAPER_RECONCILIATION_NOT_FOUND",
                "the exact Crypto PAPER reconciliation was not found",
                entity_type="CryptoPaperReconciliation",
                entity_id=reconciliation_id,
            )
        return _crypto_paper_envelope({"reconciliation": value})

    @app.get("/api/v1/operating-cycle/workspace")
    def operating_cycle_workspace() -> dict[str, object]:
        return _securities_envelope(operating_cycle_repository.workspace())

    @app.get("/api/v1/operating-cycle/plans/{plan_id}")
    def operating_cycle_plan_read(plan_id: str, request: Request) -> dict[str, object]:
        value = operating_cycle_repository.plan(plan_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "OPERATING_PLAN_NOT_FOUND",
                "the exact immutable operating-cycle plan was not found",
                entity_type="OperatingCyclePlan",
                entity_id=plan_id,
            )
        controls = operating_cycle_repository.control_events(plan_id)
        paper_activations = operating_cycle_repository.paper_activation_events(plan_id)
        return _securities_envelope(
            {
                "plan": value,
                "operator_control": controls[-1] if controls else None,
                "paper_activation": paper_activations[-1] if paper_activations else None,
                "paper_activation_history": paper_activations,
                "authority": "NONE",
                "paper_eligible": bool(paper_activations)
                and paper_activations[-1].state.value == "ENABLED",
            }
        )

    @app.post(
        "/api/v1/operating-cycle/plans/{plan_id}/paper-activation",
        dependencies=[Depends(_mutation_guard)],
    )
    def operating_cycle_paper_activation_set(
        plan_id: str,
        body: PaperActivationTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        """Governed operator action: explicitly enable or disable internal
        PAPER consideration for one exact retained plan.  Never mutates the
        plan; appends an auditable event.  Grants no provider-write
        permission and stays entirely separate from LIVE authority."""

        from apex.application.operating_cycle import SetPaperActivation
        from apex.domain.operating_cycle import OperatingCycleError, PaperActivationState

        if operating_cycle_repository.plan(plan_id) is None:
            raise _http_error(
                request,
                404,
                "OPERATING_PLAN_NOT_FOUND",
                "the exact immutable operating-cycle plan was not found",
                entity_type="OperatingCyclePlan",
                entity_id=plan_id,
            )
        try:
            event, replay = SetPaperActivation(operating_cycle_repository).execute(
                event_id=body.event_id,
                plan_id=plan_id,
                state=PaperActivationState(body.state),
                reason=body.reason,
                actor=body.actor,
                occurred_at=datetime.now(tz=UTC),
                idempotency_key=idempotency_key,
            )
        except OperatingCycleError as error:
            raise _http_error(
                request,
                409,
                str(error),
                "the PAPER activation request conflicts with retained evidence",
                entity_type="PaperActivationEvent",
                entity_id=body.event_id,
            ) from error
        return _securities_envelope(
            {
                "paper_activation_event": event,
                "idempotent_replay": replay,
                "authority": "NONE",
                "paper_eligible": event.state is PaperActivationState.ENABLED,
            }
        )

    @app.get("/api/v1/operating-cycle/cycles/{cycle_id}")
    def operating_cycle_read(cycle_id: str, request: Request) -> dict[str, object]:
        value = operating_cycle_repository.cycle(cycle_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "OPERATING_CYCLE_NOT_FOUND",
                "the exact immutable operating-cycle run was not found",
                entity_type="OperatingCycleRun",
                entity_id=cycle_id,
            )
        return _securities_envelope(
            {
                "cycle": value,
                "events": operating_cycle_repository.cycle_events(cycle_id),
                "due_work": operating_cycle_repository.due_work(cycle_id),
                "task_results": operating_cycle_repository.task_results(cycle_id),
                "authority": "NONE",
            }
        )

    @app.get("/api/v1/operating-cycle/tasks/{task_result_id}")
    def operating_task_read(task_result_id: str, request: Request) -> dict[str, object]:
        value = operating_cycle_repository.task_result(task_result_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "OPERATING_TASK_RESULT_NOT_FOUND",
                "the exact immutable operating task result was not found",
                entity_type="OperatingTaskResult",
                entity_id=task_result_id,
            )
        return _securities_envelope({"task_result": value, "authority": "NONE"})

    @app.get("/api/v1/operating-cycle/briefs/{brief_id}")
    def operating_brief_read(brief_id: str, request: Request) -> dict[str, object]:
        value = operating_cycle_repository.brief(brief_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "OPERATING_BRIEF_NOT_FOUND",
                "the exact immutable operating brief was not found",
                entity_type="OperatingBrief",
                entity_id=brief_id,
            )
        return _securities_envelope({"operating_brief": value, "authority": "NONE"})

    @app.get("/api/v1/operating-cycle/health/{health_id}")
    def operating_health_read(health_id: str, request: Request) -> dict[str, object]:
        value = operating_cycle_repository.health(health_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "OPERATING_HEALTH_NOT_FOUND",
                "the exact immutable operating-health summary was not found",
                entity_type="OperatingHealth",
                entity_id=health_id,
            )
        return _securities_envelope({"operating_health": value, "authority": "NONE"})

    @app.get("/api/v1/shared-opportunities/projections/{projection_id}")
    def shared_projection_read(projection_id: str, request: Request) -> dict[str, object]:
        value = shared_opportunity_repository.projection(projection_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "SHARED_OPPORTUNITY_PROJECTION_NOT_FOUND",
                "the exact immutable shared Opportunity projection was not found",
                entity_type="SharedOpportunityProjection",
                entity_id=projection_id,
            )
        return _securities_envelope(
            {
                "projection": value,
                "source_lineage": value.supporting_evidence_ids,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/shared-opportunities/sets/{opportunity_set_id}")
    def shared_opportunity_set_read(opportunity_set_id: str, request: Request) -> dict[str, object]:
        value = shared_opportunity_repository.opportunity_set(opportunity_set_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "SHARED_OPPORTUNITY_SET_NOT_FOUND",
                "the exact immutable shared Opportunity set was not found",
                entity_type="SharedOpportunitySet",
                entity_id=opportunity_set_id,
            )
        return _securities_envelope(
            {
                "opportunity_set": value,
                "eligibility": shared_opportunity_repository.eligibility_decisions(
                    opportunity_set_id
                ),
                "priorities": shared_opportunity_repository.priorities(opportunity_set_id),
                "explanations": shared_opportunity_repository.explanations(opportunity_set_id),
                "allocation": shared_opportunity_repository.allocation_for_set(opportunity_set_id),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/shared-opportunities/allocations/{proposal_id}")
    def shared_allocation_read(proposal_id: str, request: Request) -> dict[str, object]:
        value = shared_opportunity_repository.allocation(proposal_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "SHARED_ALLOCATION_NOT_FOUND",
                "the exact immutable hypothetical research allocation was not found",
                entity_type="HypotheticalAllocationProposal",
                entity_id=proposal_id,
            )
        return _securities_envelope(
            {
                "allocation": value,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/total-market-context/workspace")
    def total_market_context_workspace() -> dict[str, object]:
        """Read-only explanatory context beside the shared Opportunity inbox."""
        return _securities_envelope(total_market_context_repository.workspace())

    @app.get("/api/v1/total-market-context/components/{component_id}")
    def total_market_context_component_read(
        component_id: str, request: Request
    ) -> dict[str, object]:
        value = total_market_context_repository.context_component(component_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "TOTAL_MARKET_CONTEXT_COMPONENT_NOT_FOUND",
                "the exact immutable Total Market context component was not found",
                entity_type="ContextComponent",
                entity_id=component_id,
            )
        return _securities_envelope(
            {
                "component": value,
                "context_authority": "NONE",
                "financial_mutation_capability": "NONE",
            }
        )

    @app.get("/api/v1/total-market-context/snapshots/{snapshot_id}")
    def total_market_context_snapshot_read(snapshot_id: str, request: Request) -> dict[str, object]:
        value = total_market_context_repository.context_snapshot(snapshot_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "TOTAL_MARKET_CONTEXT_SNAPSHOT_NOT_FOUND",
                "the exact immutable Total Market context snapshot was not found",
                entity_type="TotalMarketContextSnapshot",
                entity_id=snapshot_id,
            )
        return _securities_envelope(
            {
                "context_snapshot": value,
                "components": total_market_context_repository.context_components(snapshot_id),
                "context_authority": "NONE",
                "financial_mutation_capability": "NONE",
            }
        )

    @app.get("/api/v1/total-market-context/enriched-sets/{enriched_set_id}")
    def total_market_context_enriched_set_read(
        enriched_set_id: str, request: Request
    ) -> dict[str, object]:
        value = total_market_context_repository.enriched_set(enriched_set_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "TOTAL_MARKET_CONTEXT_ENRICHED_SET_NOT_FOUND",
                "the exact immutable context-enriched Opportunity set was not found",
                entity_type="ContextEnrichedOpportunitySet",
                entity_id=enriched_set_id,
            )
        return _securities_envelope(
            {
                "enriched_opportunity_set": value,
                "attachments": total_market_context_repository.attachments(enriched_set_id),
                "context_authority": "NONE",
                "opportunity_authority": "NONE",
                "paper_eligible": False,
                "live_eligible": False,
                "capital_authority": "NONE",
                "order_authority": "NONE",
                "financial_mutation_capability": "NONE",
            }
        )

    @app.get("/api/v1/broad-equity/workspace")
    def broad_equity_workspace() -> dict[str, object]:
        """Read-only Broad Market capability and retained-evidence boundary."""
        retained = _retained_research_workspace(research_evidence_repository)
        universe_breakdown = _universe_evidence_breakdown(security_reference_repository)
        latest_universe = universe_breakdown["latest"]
        return _securities_envelope(
            _baseline_payload(
                {
                    "reference_contract": "APEX_SECURITY_REFERENCE_V1",
                    "reference_sources": (
                        {
                            "provider": "NASDAQ_TRADER",
                            "capability": "OFFICIAL_CURRENT_DIRECTORY",
                            "state": "READY_FOR_BOUNDED_CAPTURE",
                        },
                        {
                            "provider": "SEC_EDGAR",
                            "capability": "CURRENT_IDENTITY_AND_FILING_METADATA",
                            "state": "IDENTIFYING_USER_AGENT_REQUIRED",
                        },
                        {
                            "provider": "ALPACA",
                            "capability": "ASSET_AND_MARKET_DATA_GET_ONLY",
                            "state": "CREDENTIALS_AND_ENTITLEMENT_DEPENDENT",
                        },
                    ),
                    "security_master_count": retained["observation_count"],
                    "provider_disagreements": (),
                    "universe": latest_universe,
                    "universe_count": universe_breakdown["counts"]["universe_snapshot_count"],
                    "latest_universe_schema_compatibility": universe_breakdown[
                        "latest_schema_compatibility"
                    ],
                    **universe_breakdown["counts"],
                    "identities_requiring_review": len(
                        security_reference_repository.identities_requiring_review()
                    ),
                    # Stable-identity evidence (see docs/ARCHITECTURE.md's
                    # "Stable Security Identity and Options Relink v1"
                    # section): bounded counts only, never every retained
                    # identity/resolution row.
                    "stable_identity_count": security_identity_repository.stable_identity_count(),
                    "identity_resolution_count": security_identity_repository.resolution_count(),
                    "identity_resolution_state_counts": (
                        security_identity_repository.resolution_state_counts()
                    ),
                    "identity_ambiguous_review_href": "/api/v1/security-identity/ambiguous",
                    "retained_research": retained,
                    "retained_snapshots": retained["snapshots"],
                    "retained_experiment_runs": retained["runs"],
                    "retained_outcomes": retained["outcomes"],
                    "eligibility_waterfall": (
                        latest_universe.research_exclusion_counts
                        if latest_universe is not None
                        else ()
                    ),
                    "execution_capability_waterfall": (
                        latest_universe.execution_capability_counts
                        if latest_universe is not None
                        else ()
                    ),
                    "data_capture": None,
                    "data_quality": (),
                    "regime": (
                        "RETAINED_RESEARCH_EXPERIMENTS"
                        if retained["runs"]
                        else "NO_RETAINED_BROAD_DISCOVERY"
                    ),
                    "ranked_securities": (),
                    "hypothetical_portfolio": (),
                    "shadow_state": "NO_ACTIVE_PLAN",
                    "broad_market_reference_summary": _broad_market_reference_summary(
                        security_reference_repository,
                        broad_equity_repository,
                        options_intelligence_repository,
                        research_evidence_repository,
                        security_identity_repository,
                        historical_market_data_repository,
                        genuine_multi_asset_repository,
                        historical_replay_repository,
                        corporate_fundamentals_repository,
                    ),
                    "warnings": (
                        "CURRENT-LISTED UNIVERSE — NOT HISTORICAL POINT-IN-TIME MEMBERSHIP",
                        "BROAD EQUITY DISCOVERY IS RESEARCH ONLY",
                        "OUT_OF_DOMAIN_DIAGNOSTIC",
                    ),
                    "authority": "RESEARCH_ONLY",
                    "financial_authority": "NONE",
                }
            )
        )

    @app.get("/api/v1/corporate-fundamentals/workspace")
    def corporate_fundamentals_workspace() -> dict[str, object]:
        """Read-only corporate filing/fundamental-fact research boundary --
        official SEC filing/fact evidence only; never stock-picking,
        strategy generation, or an execution surface."""
        from apex.application.corporate_fundamentals import issuer_quality_assessments_for_plan
        from apex.domain.corporate_fundamentals import (
            CONCEPT_DEFINITIONS,
            CONCEPT_VOCABULARY_VERSION,
            EARNINGS_TIMING_LIMITATION,
            RETAINED_FILING_FORMS,
            LookbackWindow,
            corporate_fundamentals_warnings,
            pending_members,
        )

        plan = corporate_fundamentals_repository.latest_plan()
        checkpoints = (
            corporate_fundamentals_repository.checkpoints_for_plan(plan.plan_id)
            if plan is not None
            else ()
        )
        latest_checkpoint = checkpoints[-1] if checkpoints else None
        processed_ciks = (
            corporate_fundamentals_repository.processed_ciks(plan.plan_id)
            if plan is not None
            else ()
        )
        remaining = pending_members(plan, processed_ciks=processed_ciks) if plan is not None else ()
        latest_quality_report = (
            corporate_fundamentals_repository.latest_quality_report(plan.plan_id)
            if plan is not None
            else None
        )
        latest_dataset = (
            corporate_fundamentals_repository.latest_dataset_snapshot(plan.plan_id)
            if plan is not None
            else None
        )
        assessments = (
            issuer_quality_assessments_for_plan(
                repository=corporate_fundamentals_repository, plan=plan
            )
            if plan is not None
            else ()
        )
        effective_lookback = (
            LookbackWindow.create(
                lookback_years=plan.bounds.lookback_years,
                reference_date=datetime.now(tz=UTC).date(),
            )
            if plan is not None
            else None
        )
        calibration_plan = (corporate_calibration_depth_repository.list_plans() or (None,))[-1]
        calibration_run = (
            corporate_calibration_depth_repository.run_by_calibration(
                calibration_plan.calibration_id
            )
            if calibration_plan is not None
            else None
        )
        calibration_report = (
            corporate_calibration_depth_repository.report_by_calibration(
                calibration_plan.calibration_id
            )
            if calibration_plan is not None
            else None
        )
        calibration_reassessments = (
            corporate_calibration_depth_repository.reassessments_for_calibration(
                calibration_plan.calibration_id
            )
            if calibration_plan is not None
            else ()
        )
        calibration_follow_up_qualifications = (
            corporate_calibration_depth_repository.follow_up_qualifications_for_calibration(
                calibration_plan.calibration_id
            )
            if calibration_plan is not None
            else ()
        )
        return _securities_envelope(
            {
                "reference_contract": "CORPORATE_FUNDAMENTAL_INTELLIGENCE_V1",
                "sec_sources": (
                    {
                        "provider": "SEC_EDGAR",
                        "endpoint": "submissions",
                        "capability": "FILING_HISTORY",
                    },
                    {
                        "provider": "SEC_EDGAR",
                        "endpoint": "companyfacts",
                        "capability": "STRUCTURED_XBRL_FACTS",
                    },
                ),
                "concept_vocabulary_version": CONCEPT_VOCABULARY_VERSION,
                "concept_vocabulary": tuple(item.concept.value for item in CONCEPT_DEFINITIONS),
                "retained_filing_forms": tuple(sorted(RETAINED_FILING_FORMS)),
                "plan_id": plan.plan_id if plan is not None else None,
                "plan_member_count": len(plan.members) if plan is not None else 0,
                "excluded_missing_cik": plan.excluded_missing_cik if plan is not None else (),
                "checkpoint_count": len(checkpoints),
                "latest_checkpoint": latest_checkpoint,
                "processed_issuer_count": (
                    len(corporate_fundamentals_repository.processed_ciks(plan.plan_id))
                    if plan is not None
                    else 0
                ),
                "capture_complete": (
                    bool(latest_checkpoint.complete) if latest_checkpoint is not None else False
                ),
                "remaining_issuer_count": len(remaining),
                "remaining_issuer_symbols": tuple(sorted(m.symbol for m in remaining)),
                "effective_lookback": effective_lookback,
                "latest_quality_report": latest_quality_report,
                "latest_dataset_snapshot": latest_dataset,
                "issuer_quality_assessments": assessments,
                "replay_attachment_summary": (
                    corporate_fundamentals_repository.attachment_run_summary()
                ),
                "point_in_time_limitation": (
                    "FILING_ACCEPTANCE_TIME_USED_FOR_POINT_IN_TIME_VISIBILITY_"
                    "FILING_DATE_ONLY_WHEN_ABSENT"
                ),
                "earnings_event_limitation": EARNINGS_TIMING_LIMITATION,
                "empty_state": "NO_RETAINED_CORPORATE_EVIDENCE_PLAN" if plan is None else None,
                "warnings": corporate_fundamentals_warnings(),
                # Corporate Research Calibration Depth v1 (methodology calibration over the
                # already-retained Cross-Market Research v3 agenda) -- extends this existing
                # workspace view rather than adding a new dashboard, per that milestone's own
                # section 27; every field below stays authority NONE.
                "corporate_calibration_depth_summary": {
                    "calibration_id": (
                        calibration_plan.calibration_id if calibration_plan is not None else None
                    ),
                    "strata_count": (
                        len(calibration_plan.stratum_targets) if calibration_plan is not None else 0
                    ),
                    "controls_per_stratum": (
                        calibration_plan.controls_per_stratum
                        if calibration_plan is not None
                        else None
                    ),
                    "total_new_control_budget": (
                        calibration_plan.total_new_control_budget
                        if calibration_plan is not None
                        else None
                    ),
                    "run_status": calibration_run.status.value
                    if calibration_run is not None
                    else None,
                    "controls_completed": (
                        len(calibration_run.completed_spec_ids)
                        if calibration_run is not None
                        else 0
                    ),
                    "report_available": calibration_report is not None,
                    "raw_false_positive_rate": (
                        calibration_report.raw_false_positive_rate
                        if calibration_report is not None
                        else None
                    ),
                    "corrected_false_positive_rate": (
                        calibration_report.corrected_false_positive_rate
                        if calibration_report is not None
                        else None
                    ),
                    "false_validated_rate": (
                        calibration_report.false_validated_rate
                        if calibration_report is not None
                        else None
                    ),
                    "policy_v2_justified": (
                        calibration_report.policy_v2_justified
                        if calibration_report is not None
                        else None
                    ),
                    "genuine_reassessment_count": len(calibration_reassessments),
                    "follow_up_qualification_count": len(calibration_follow_up_qualifications),
                    "empty_state": (
                        "NO_RETAINED_CALIBRATION_DEPTH_PLAN" if calibration_plan is None else None
                    ),
                    "authority": "NONE",
                },
                # Corporate Calibration-Qualified Regime Follow-Up v1 -- the
                # single calibration-qualified SPLIT_BY_REGIME follow-up's
                # frozen specification, regime split, null calibration,
                # stability, incremental contribution, conclusion, evidence
                # grade, and independent-confirmation requirement. Extends
                # this existing workspace view rather than adding a new
                # dashboard, per that milestone's own section 23; every
                # field below stays authority NONE.
                "corporate_regime_followup_summary": _corporate_regime_followup_summary(
                    corporate_regime_followup_repository
                ),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/options-intelligence/workspace")
    def options_intelligence_workspace() -> dict[str, object]:
        """Read-only defined-risk options capability; no execution controls."""
        from apex.adapters.options_reference import (
            MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
            NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
        )
        from apex.application.research_evidence import (
            NASDAQ_OPTIONS_PROVIDER,
            latest_completed_capture,
            option_underlying_rejection_from_observation,
        )

        # Scope to exactly the latest universe's own identities -- see the
        # identical comment in _broad_market_reference_summary. Without
        # this, a superseded universe build's decisions (retained under
        # canonical_security_id values the latest universe no longer uses)
        # would silently inflate this otherwise latest-universe-scoped view.
        latest_universe = _universe_evidence_breakdown(security_reference_repository)["latest"]
        optionable = options_intelligence_repository.optionable_underlyings()
        if latest_universe is not None:
            latest_security_ids = {item.canonical_security_id for item in latest_universe.decisions}
            optionable = tuple(
                item for item in optionable if item.canonical_security_id in latest_security_ids
            )
        contract_count = options_intelligence_repository.contract_count()
        capabilities = options_intelligence_repository.capabilities()
        options_capture = latest_completed_capture(
            research_evidence_repository.captures(), NASDAQ_OPTIONS_PROVIDER
        )
        rejections = (
            tuple(
                option_underlying_rejection_from_observation(item)
                for item in research_evidence_repository.observations(options_capture.capture_id)
            )
            if options_capture is not None
            else ()
        )
        latest_stable_linkage_run_id = security_identity_repository.latest_option_linkage_run_id()
        stable_linkage_counts = _stable_linkage_counts(security_identity_repository)
        return _securities_envelope(
            _baseline_payload(
                {
                    "contract": "APEX_OPTION_CONTRACT_V1",
                    "selected_underlying": None,
                    "stock_ranking": None,
                    "chain_snapshot": None,
                    "expirations": (),
                    "contracts_retained": contract_count,
                    "optionable_underlyings_considered": len(optionable),
                    "optionable_underlying_count": sum(item.optionable for item in optionable),
                    "options_source_schema_level": NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
                    "options_capture_bound_bytes": MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
                    "options_capture_actual_bytes": (
                        options_capture.byte_count if options_capture is not None else None
                    ),
                    "options_capture_raw_sha256": (
                        options_capture.raw_sha256 if options_capture is not None else None
                    ),
                    "options_capture_status": (
                        options_capture.status.value if options_capture is not None else None
                    ),
                    # Legacy linkage: canonical_security_id-keyed, baked in at
                    # original capture time against whichever universe was
                    # live then -- unstable across source-preference changes.
                    "linked_underlying_count": sum(item.optionable for item in optionable),
                    "unlinked_count": sum(
                        1 for item in rejections if item.outcome.value == "UNLINKED"
                    ),
                    "ambiguous_count": sum(
                        1 for item in rejections if item.outcome.value == "AMBIGUOUS"
                    ),
                    # Current stable-identity relink: the latest
                    # security-identity-options-relink run over these same
                    # retained contracts (no recapture), against the stable
                    # identity space -- see docs/ARCHITECTURE.md's "Stable
                    # Security Identity and Options Relink v1" section.
                    "latest_stable_linkage_run_id": latest_stable_linkage_run_id,
                    "stable_linked_underlying_count": stable_linkage_counts["linked"],
                    "stable_unlinked_count": stable_linkage_counts["unlinked"],
                    "stable_ambiguous_count": stable_linkage_counts["ambiguous"],
                    "stable_linkage_unresolved_href": "/api/v1/security-identity/ambiguous",
                    "defined_risk_candidates": (),
                    "stock_option_comparisons": (),
                    "shadow_proposal_state": "NO_RETAINED_PROPOSAL",
                    "quote_chain_intelligence": _options_quote_chain_summary(
                        options_quote_intelligence_repository
                    ),
                    "structure_economics": _options_structures_summary(
                        options_structures_repository, options_quote_intelligence_repository
                    ),
                    "forward_evidence_operations": _options_research_operations_summary(
                        options_research_operations_repository,
                        options_quote_intelligence_repository,
                    ),
                    "retained_capabilities": capabilities,
                    "capabilities": (
                        {
                            "provider": "ALPACA",
                            "contract_discovery": "AVAILABLE_WHEN_ENTITLED",
                            "quotes": "AVAILABLE_WHEN_ENTITLED",
                            "greeks": "FIELD_MAY_BE_UNAVAILABLE",
                            "option_order_authority": "NONE",
                        },
                        {
                            "provider": "OCC",
                            "complete_series": "PAID_REFERENCE_REQUIRED",
                            "open_interest": "PAID_REFERENCE_REQUIRED",
                        },
                    ),
                    "warnings": (
                        "OPTIONS INTELLIGENCE IS RESEARCH ONLY",
                        "OPTIONS CAN EXPIRE WORTHLESS",
                        "ONLY DEFINED-RISK STRUCTURES ARE INCLUDED IN V1",
                        "NO OPTION ORDER AUTHORITY IS ENABLED",
                        "LIVE TRADING IS NOT ENABLED",
                    ),
                    "authority": "OBSERVATION_ONLY",
                    "financial_authority": "NONE",
                }
            )
        )

    @app.get("/api/v1/security-identity/resolve/{identity_id}")
    def security_identity_resolve(identity_id: str, request: Request) -> dict[str, object]:
        """Resolve any ID Apex has ever produced -- a legacy
        ``canonical_security_id`` from any retained universe build, or an
        earlier ``stable_security_id`` -- to what it currently means.

        ``as_old_identity`` is populated when ``identity_id`` was itself
        superseded/aliased/found ambiguous; ``as_resolved_stable_identity``
        is populated when ``identity_id`` *is* a stable id other identities
        resolve to (its known aliases/predecessors). Both empty means this
        exact ID has no retained resolution evidence at all.
        """
        as_old = security_identity_repository.resolutions_for_old_id(identity_id)
        as_resolved = security_identity_repository.resolutions_to_stable_id(identity_id)
        if not as_old and not as_resolved:
            raise _http_error(
                request,
                404,
                "SECURITY_IDENTITY_NOT_FOUND",
                "no retained resolution evidence exists for this exact identity id",
                entity_type="IdentityResolution",
                entity_id=identity_id,
            )
        return _securities_envelope(
            {
                "id": identity_id,
                "as_old_identity": as_old,
                "as_resolved_stable_identity": as_resolved,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/security-identity/ambiguous")
    def security_identity_ambiguous(request: Request) -> dict[str, object]:
        """Bounded review queue: every identity resolution this milestone
        could not resolve with confidence, never silently merged or split.
        """
        limit = 200
        raw_limit = request.query_params.get("limit")
        if raw_limit is not None:
            try:
                limit = max(0, min(200, int(raw_limit)))
            except ValueError:
                raise _http_error(
                    request,
                    400,
                    "SECURITY_IDENTITY_LIMIT_INVALID",
                    "limit must be an integer",
                ) from None
        ambiguous = security_identity_repository.ambiguous_resolutions(limit=limit)
        return _securities_envelope(
            {
                "limit": limit,
                "count": len(ambiguous),
                "ambiguous": ambiguous,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        )

    @app.get("/api/v1/external-paper/readiness")
    def external_paper_readiness() -> dict[str, object]:
        """Fixture-only readiness; this route never reads credentials or a broker."""
        return _securities_envelope(
            _baseline_payload(
                {
                    "environment": "FIXTURE_PAPER",
                    "adapter": fixture_paper_broker.identity,
                    "credentials_available": False,
                    "readiness": "FIXTURE_READY",
                    "authority": "NONE",
                    "kill_state": "NOT_AUTHORIZED",
                    "order_submit": True,
                    "order_read": True,
                    "cancel": True,
                    "warnings": (
                        "PAPER EXECUTION IS SIMULATED",
                        "EXTERNAL BROKER PAPER STATE IS EVIDENCE, NOT AUTOMATIC APEX TRUTH",
                        "LIVE TRADING IS NOT ENABLED",
                    ),
                }
            )
        )

    @app.get("/api/v1/external-paper/acceptance/campaigns/{campaign_id}")
    def alpaca_paper_acceptance_campaign_status(
        campaign_id: str, request: Request
    ) -> dict[str, object]:
        """Read retained engineering acceptance state without broker access."""
        acceptance = external_paper_repository.acceptance_for_campaign(campaign_id)
        if acceptance is None:
            raise _http_error(
                request,
                404,
                "ALPACA_PAPER_ACCEPTANCE_CAMPAIGN_NOT_FOUND",
                "acceptance Campaign was not found",
            )
        return _securities_envelope(
            _baseline_payload(
                {
                    "environment": "ALPACA_PAPER",
                    "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
                    "acceptance": acceptance,
                    "preflight": external_paper_repository.latest_acceptance_preflight(
                        acceptance.instruction_id
                    ),
                    "actual_paper_order_submitted": False,
                    "live_authority": "NONE",
                }
            )
        )

    @app.get("/api/v1/external-paper/alpaca/instructions/{instruction_id}/status")
    def alpaca_paper_instruction_status(instruction_id: str, request: Request) -> dict[str, object]:
        """Retained-state genuine PAPER view.  It never contacts a broker."""
        instruction = external_paper_repository.instruction(instruction_id)
        if instruction is None:
            raise _http_error(
                request, 404, "EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND", "instruction was not found"
            )
        evidence = external_paper_repository.evidence_for_instruction(instruction_id)
        validated = external_paper_repository.validated_fills_for_instruction(instruction_id)
        binding = external_paper_repository.binding_for_instruction(instruction_id)
        order = (
            securities_paper_repository.order(binding.internal_order_id)
            if binding is not None
            and securities_paper_repository.has_order(binding.internal_order_id)
            else None
        )
        position = (
            securities_paper_repository.position(order.position_id)
            if order is not None and order.position_id is not None
            else None
        )
        latest = next(
            (item for item in reversed(evidence) if item.observation_type == "ORDER_STATUS"), None
        )
        return _securities_envelope(
            _baseline_payload(
                {
                    "environment": "ALPACA_PAPER",
                    "instruction": instruction,
                    "broker_order_id": instruction.broker_order_id,
                    "latest_broker_state": latest.sanitized_payload.get("status")
                    if latest
                    else None,
                    "observations": evidence,
                    "validated_fills": tuple(
                        {
                            "fill": fill,
                            "application": external_paper_repository.fill_application(
                                fill.validated_fill_id
                            ),
                        }
                        for fill in validated
                    ),
                    "internal_order": order,
                    "position": position,
                    "reconciliations": external_paper_repository.reconciliations_for_instruction(
                        instruction_id
                    ),
                    "kill_state": "KILLED"
                    if external_paper_repository.external_paper_killed(instruction.campaign_id)
                    else "OPEN",
                    "live_authority": "NONE",
                }
            )
        )

    @app.post(
        "/api/v1/external-paper/alpaca/instructions/{instruction_id}/synchronize",
        dependencies=[Depends(_mutation_guard)],
    )
    def synchronize_alpaca_paper_instruction(
        instruction_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            result = SynchronizeAlpacaPaperInstruction(
                repository=external_paper_repository, broker=AlpacaPaperBroker()
            ).execute(instruction_id=instruction_id)
            return _securities_envelope(_baseline_payload({"result": result}))
        except ExternalPaperApplicationError as error:
            raise _http_error(request, 422, "ALPACA_PAPER_SYNC_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/external-paper/alpaca/instructions/{instruction_id}/validate-fills",
        dependencies=[Depends(_mutation_guard)],
    )
    def validate_alpaca_paper_fill(
        instruction_id: str,
        body: ExternalPaperFillValidationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            fill = ValidateAlpacaPaperFill(repository=external_paper_repository).execute(
                instruction_id=instruction_id, evidence_id=body.evidence_id
            )
            return _securities_envelope(
                _baseline_payload({"validated_fill": fill, "live_authority": "NONE"})
            )
        except (ExternalPaperApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "ALPACA_PAPER_FILL_VALIDATION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/external-paper/alpaca/instructions/{instruction_id}/apply-fills",
        dependencies=[Depends(_mutation_guard)],
    )
    def apply_alpaca_paper_fills(
        instruction_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            result = ProcessAlpacaPaperFills(
                repository=external_paper_repository,
                securities_repository=securities_paper_repository,
            ).execute(instruction_id=instruction_id)
            return _securities_envelope(
                _baseline_payload({"result": result, "live_authority": "NONE"})
            )
        except ExternalPaperApplicationError as error:
            raise _http_error(
                request, 422, "ALPACA_PAPER_FILL_APPLICATION_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/external-paper/acceptance/instructions/{instruction_id}")
    def alpaca_paper_acceptance_instruction_status(
        instruction_id: str, request: Request
    ) -> dict[str, object]:
        acceptance = external_paper_repository.acceptance_for_instruction(instruction_id)
        if acceptance is None:
            raise _http_error(
                request,
                404,
                "ALPACA_PAPER_ACCEPTANCE_INSTRUCTION_NOT_FOUND",
                "acceptance instruction was not found",
            )
        instruction = external_paper_repository.instruction(instruction_id)
        binding = external_paper_repository.binding_for_instruction(instruction_id)
        return _securities_envelope(
            _baseline_payload(
                {
                    "environment": "ALPACA_PAPER",
                    "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
                    "acceptance": acceptance,
                    "instruction": instruction,
                    "binding": binding,
                    "preflight": external_paper_repository.latest_acceptance_preflight(
                        instruction_id
                    ),
                    "actual_paper_order_submitted": False,
                    "live_authority": "NONE",
                }
            )
        )

    @app.post(
        "/api/v1/external-paper/instructions",
        dependencies=[Depends(_mutation_guard)],
    )
    def create_external_paper_instruction(
        body: ExternalPaperInstructionCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            authorization = external_paper_repository.authorization(body.authorization_id)
            if authorization is None:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_AUTHORIZATION_NOT_FOUND")
            instruction = CreateAuthorizedExternalPaperInstruction(
                repository=external_paper_repository
            ).execute(
                instruction=ExternalPaperInstruction.authorized(
                    authorization=authorization,
                    symbol=body.symbol,
                    side=body.side,
                    quantity=body.quantity,
                    maximum_notional=body.maximum_notional,
                    expires_at=body.expires_at,
                    idempotency_key=idempotency_key,
                ),
                now=datetime.now(tz=UTC),
            )
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction": instruction,
                        "state": instruction.state.value,
                        "next_action": "PREPARE_INTERNAL_EXECUTION",
                        "broker_submission": False,
                        "live_authority": "NONE",
                    }
                )
            )
        except (ExternalPaperApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_INSTRUCTION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/prepare",
        dependencies=[Depends(_mutation_guard)],
    )
    def prepare_external_paper_instruction(
        instruction_id: str,
        body: ExternalPaperPreparationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            binding = external_paper_repository.binding_for_instruction(instruction_id)
            if binding is None or binding.binding_id != body.binding_id:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_BINDING_NOT_FOUND")
            prepared = PrepareAuthorizedExternalPaperExecution(
                repository=external_paper_repository,
                securities_repository=securities_paper_repository,
            ).execute(binding=binding, now=datetime.now(tz=UTC))
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction_id": instruction_id,
                        "binding": prepared,
                        "state": prepared.state.value,
                        "blocker": None,
                        "next_action": "SUBMIT_FIXTURE_INSTRUCTION",
                        "broker_submission": False,
                        "live_authority": "NONE",
                    }
                )
            )
        except (ExternalPaperApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_PREPARATION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/submit",
        dependencies=[Depends(_mutation_guard)],
    )
    def submit_external_paper_instruction(
        instruction_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            result = SubmitAuthorizedPaperInstruction(
                repository=external_paper_repository, broker=fixture_paper_broker
            ).execute(
                instruction_id=instruction_id,
                capability=ready_capability(datetime.now(tz=UTC)),
                now=datetime.now(tz=UTC),
            )
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction": result.instruction,
                        "evidence": result.evidence,
                        "state": result.instruction.state.value,
                        "replayed": result.replayed,
                        "blocker": result.instruction.reason_code,
                        "next_action": "SYNCHRONIZE_FIXTURE_INSTRUCTION",
                        "broker_submission": "FIXTURE_ONLY",
                        "live_authority": "NONE",
                    }
                )
            )
        except ExternalPaperApplicationError as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_SUBMISSION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/synchronize",
        dependencies=[Depends(_mutation_guard)],
    )
    def synchronize_external_paper_instruction(
        instruction_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            instruction, reconciliation = SyncExternalPaperInstruction(
                repository=external_paper_repository,
                broker=fixture_paper_broker,
                securities_repository=securities_paper_repository,
            ).execute(instruction_id=instruction_id)
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction": instruction,
                        "reconciliation": reconciliation,
                        "state": instruction.state.value,
                        "blocker": reconciliation.required_action,
                        "next_action": "VALIDATE_RETAINED_FILL_OR_RECONCILE",
                        "broker_read": "FIXTURE_ONLY",
                        "live_authority": "NONE",
                    }
                )
            )
        except ExternalPaperApplicationError as error:
            raise _http_error(request, 422, "EXTERNAL_PAPER_SYNC_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/cancel",
        dependencies=[Depends(_mutation_guard)],
    )
    def cancel_external_paper_instruction(
        instruction_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key
        try:
            evidence = CancelExternalPaperInstruction(
                repository=external_paper_repository, broker=fixture_paper_broker
            ).execute(instruction_id=instruction_id)
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction_id": instruction_id,
                        "evidence": evidence,
                        "state": "CANCELLATION_RETAINED",
                        "blocker": None,
                        "next_action": "SYNCHRONIZE_FIXTURE_CANCELLATION",
                        "broker_cancel": "FIXTURE_ONLY",
                        "live_authority": "NONE",
                    }
                )
            )
        except ExternalPaperApplicationError as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_CANCELLATION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/reduce",
        dependencies=[Depends(_mutation_guard)],
    )
    def prepare_external_paper_reduction(
        instruction_id: str,
        body: ExternalPaperReductionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key  # Instruction identity supplies exact idempotency for preparation.
        try:
            prepared = PrepareAuthorizedExternalPaperReduction(
                repository=external_paper_repository,
                securities_repository=securities_paper_repository,
            ).execute(
                instruction_id=instruction_id,
                position_id=body.position_id,
                decision_at=body.decision_at,
                earliest_fill_at=body.earliest_fill_at,
                expires_at=body.expires_at,
            )
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction_id": instruction_id,
                        "binding": prepared.binding,
                        "internal_order": prepared.order,
                        "state": "PREPARED",
                        "next_action": "SUBMIT_ONLY_AFTER_SEPARATE_READY_CAPABILITY_CHECK",
                        "broker_submission": False,
                        "live_authority": "NONE",
                    }
                )
            )
        except (ExternalPaperApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_REDUCTION_REJECTED", str(error)
            ) from error

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/validate-fills",
        dependencies=[Depends(_mutation_guard)],
    )
    def validate_external_paper_fill(
        instruction_id: str,
        body: ExternalPaperFillValidationRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key  # Immutable evidence identity controls validation idempotency.
        try:
            evidence = external_paper_repository.broker_evidence(body.evidence_id)
            if evidence is None or evidence.instruction_id != instruction_id:
                raise ExternalPaperApplicationError("EXTERNAL_PAPER_EVIDENCE_NOT_FOUND")
            validated = ValidateExternalPaperFill(repository=external_paper_repository).execute(
                evidence=evidence
            )
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction_id": instruction_id,
                        "evidence_id": body.evidence_id,
                        "validated_fill": validated,
                        "state": validated.state.value,
                        "next_action": "APPLY_VALIDATED_FILL",
                        "broker_submission": False,
                        "broker_read": False,
                        "live_authority": "NONE",
                    }
                )
            )
        except (ExternalPaperApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_FILL_VALIDATION_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/external-paper/instructions/{instruction_id}/reconciliation")
    def external_paper_reconciliation_report(
        instruction_id: str,
        request: Request,
    ) -> dict[str, object]:
        if external_paper_repository.instruction(instruction_id) is None:
            raise _http_error(
                request,
                404,
                "EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND",
                "external PAPER instruction was not found",
            )
        reports = external_paper_repository.reconciliations_for_instruction(instruction_id)
        return _securities_envelope(
            _baseline_payload(
                {
                    "instruction_id": instruction_id,
                    "reconciliations": reports,
                    "state": "RETAINED_RECONCILIATION_REPORT",
                    "next_action": (
                        "RETAINED_RECONCILIATION_AVAILABLE"
                        if reports
                        else "SYNCHRONIZE_THROUGH_APPROVED_BROKER_BOUNDARY"
                    ),
                    "broker_submission": False,
                    "broker_read": False,
                    "live_authority": "NONE",
                }
            )
        )

    @app.get("/api/v1/external-paper/instructions/{instruction_id}/fill-timeline")
    def external_paper_fill_timeline(
        instruction_id: str,
        request: Request,
    ) -> dict[str, object]:
        if external_paper_repository.instruction(instruction_id) is None:
            raise _http_error(
                request,
                404,
                "EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND",
                "external PAPER instruction was not found",
            )
        fills = external_paper_repository.validated_fills_for_instruction(instruction_id)
        return _securities_envelope(
            _baseline_payload(
                {
                    "instruction_id": instruction_id,
                    "timeline": tuple(
                        {
                            "validated_fill": fill,
                            "application": external_paper_repository.fill_application(
                                fill.validated_fill_id
                            ),
                        }
                        for fill in fills
                    ),
                    "state": "RETAINED_FILL_TIMELINE",
                    "blocker": None,
                    "next_action": "APPLY_VALIDATED_INCREMENT"
                    if any(
                        external_paper_repository.fill_application(fill.validated_fill_id) is None
                        for fill in fills
                    )
                    else "RECONCILE_RETAINED_OBSERVATION",
                    "broker_read": False,
                    "live_authority": "NONE",
                }
            )
        )

    @app.post(
        "/api/v1/external-paper/instructions/{instruction_id}/apply-fills",
        dependencies=[Depends(_mutation_guard)],
    )
    def apply_external_paper_fills(
        instruction_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        del idempotency_key  # Immutable validated-fill identity controls accounting idempotency.
        try:
            result = ProcessExternalPaperFills(
                repository=external_paper_repository,
                securities_repository=securities_paper_repository,
            ).execute(instruction_ids=(instruction_id,))[0]
            return _securities_envelope(
                _baseline_payload(
                    {
                        "instruction_id": result.instruction_id,
                        "validated_fill_ids": result.validated_fill_ids,
                        "applied_fill_ids": result.applied_fill_ids,
                        "blocker": result.blocker,
                        "next_action": (
                            "RECONCILE_RETAINED_BROKER_OBSERVATION"
                            if result.blocker is None
                            else "RESOLVE_RETAINED_FILL_BLOCKER"
                        ),
                        "broker_submission": False,
                        "broker_read": False,
                        "live_authority": "NONE",
                    }
                )
            )
        except ExternalPaperApplicationError as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_FILL_APPLICATION_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/external-paper/campaigns/{campaign_id}/status")
    def external_paper_campaign_status(campaign_id: str) -> dict[str, object]:
        instructions = external_paper_repository.instructions_for_campaign(campaign_id)

        def instruction_state(item: ExternalPaperInstruction) -> dict[str, object]:
            instruction = item
            authorization = external_paper_repository.authorization(instruction.authorization_id)
            binding = external_paper_repository.binding_for_instruction(instruction.instruction_id)
            order = (
                securities_paper_repository.order(binding.internal_order_id)
                if binding is not None
                and securities_paper_repository.has_order(binding.internal_order_id)
                else None
            )
            position = (
                securities_paper_repository.position(order.position_id)
                if order is not None and order.position_id is not None
                else None
            )
            attempt = external_paper_repository.submission_attempt(instruction.instruction_id)
            evidence = external_paper_repository.evidence_for_instruction(
                instruction.instruction_id
            )
            validated_fills = external_paper_repository.validated_fills_for_instruction(
                instruction.instruction_id
            )
            return {
                "instruction_id": instruction.instruction_id,
                "authorization": authorization,
                "symbol": instruction.symbol,
                "side": instruction.side,
                "quantity": str(instruction.quantity),
                "state": instruction.state.value,
                "broker_order_id": instruction.broker_order_id,
                "expires_at": instruction.expires_at,
                "binding": binding,
                "internal_order": order,
                "position": position,
                "submission_attempt": attempt,
                "broker_observations": evidence,
                "validated_fills": tuple(
                    {
                        "fill": fill,
                        "application": external_paper_repository.fill_application(
                            fill.validated_fill_id
                        ),
                    }
                    for fill in validated_fills
                ),
                "reconciliations": external_paper_repository.reconciliations_for_instruction(
                    instruction.instruction_id
                ),
            }

        return _securities_envelope(
            _baseline_payload(
                {
                    "campaign_id": campaign_id,
                    "environment": "PAPER",
                    "kill_state": "KILLED"
                    if external_paper_repository.external_paper_killed(campaign_id)
                    else "OPENINGS_NOT_AUTHORIZED",
                    "unknown_broker_activity_block": (
                        external_paper_repository.has_unknown_broker_activity(campaign_id)
                    ),
                    "instruction_count": len(instructions),
                    "instructions": tuple(instruction_state(item) for item in instructions),
                    "live_authority": "NONE",
                }
            )
        )

    @app.post(
        "/api/v1/external-paper/campaigns/{campaign_id}/kill",
        dependencies=[Depends(_mutation_guard)],
    )
    def kill_external_paper_campaign(
        campaign_id: str,
        body: ExternalPaperKillRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            replayed = KillExternalPaper(repository=external_paper_repository).execute(
                campaign_id=campaign_id,
                reason=body.reason,
                confirmation_text=body.confirmation,
                operator_action_id=f"api:{idempotency_key}",
            )
            return _securities_envelope(
                _baseline_payload(
                    {
                        "campaign_id": campaign_id,
                        "state": "KILLED",
                        "replayed": replayed,
                        "next_action": "SYNC_OR_REDUCE_EXISTING_EXTERNAL_PAPER_STATE",
                        "live_authority": "NONE",
                    }
                )
            )
        except ExternalPaperApplicationError as error:
            raise _http_error(request, 422, "EXTERNAL_PAPER_KILL_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/external-paper/campaigns/{campaign_id}/flatten",
        dependencies=[Depends(_mutation_guard)],
    )
    def flatten_external_paper_campaign(
        campaign_id: str,
        body: ExternalPaperFlattenRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            result = FlattenExternalPaperCampaign(
                repository=external_paper_repository,
                broker=fixture_paper_broker,
                securities_repository=securities_paper_repository,
            ).execute(
                campaign_id=campaign_id,
                reason=body.reason,
                confirmation_text=body.confirmation,
                operator_action_id=f"api:{idempotency_key}",
            )
            return _securities_envelope(_baseline_payload(asdict(result)))
        except ExternalPaperApplicationError as error:
            raise _http_error(
                request, 422, "EXTERNAL_PAPER_FLATTEN_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/securities/simulator-conformance/{report_id}")
    def canonical_simulator_conformance_detail(
        report_id: str,
        request: Request,
    ) -> dict[str, object]:
        try:
            return _securities_envelope(
                _baseline_payload(securities_baseline_repository.conformance_detail(report_id))
            )
        except SecuritiesBaselineApplicationError as error:
            raise _http_error(
                request,
                404,
                "SECURITIES_SIMULATOR_CONFORMANCE_NOT_FOUND",
                str(error),
            ) from error

    @app.get("/api/v1/securities/simulator-status")
    def canonical_simulator_status() -> dict[str, object]:
        vectorbt = VectorbtSecuritiesAdapter(Path(".local") / "securities" / "simulator-results")
        lean = LeanCanonicalSecuritiesAdapter()
        return _securities_envelope(
            _baseline_payload(
                {
                    "items": {
                        "APEX_RESEARCH": (True, "APEX_RESEARCH_V1"),
                        "APEX_PAPER": (
                            True,
                            "APEX_PAPER_STAGE2_GOVERNED_NOT_EXECUTED",
                        ),
                        "VECTORBT": vectorbt.availability(),
                        "LEAN": lean.availability(),
                        "DETERMINISTIC_FIXTURE": (
                            True,
                            "PROTOCOL_FIXTURE_NOT_EXTERNAL_ENGINE",
                        ),
                    }
                }
            )
        )

    @app.post(
        "/api/v1/securities/baseline-suites",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def run_canonical_baseline_suite(
        body: SecuritiesBaselineSuiteRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            securities_campaigns.assert_research_allowed(body.campaign_id)
            baselines = RegisterCanonicalSecuritiesBaselines(
                securities_baseline_repository
            ).execute(created_at=datetime.now(tz=UTC))
            simulators = (
                ApexResearchSimulatorAdapter(),
                VectorbtSecuritiesAdapter(Path(".local") / "securities" / "simulator-results"),
                LeanCanonicalSecuritiesAdapter(),
            )
            runner = RunCanonicalSecuritiesBaseline(
                repository=securities_baseline_repository,
                data=securities_repository,
                simulators=simulators,
            )
            runs = []
            batch = securities_repository.get_batch(body.data_batch_id)
            canonical_requests = {
                baseline.baseline_id: CanonicalSecuritiesSimulationRequest.create(
                    baseline=baseline,
                    data_batch_id=batch.batch_id,
                    data_batch_hash=batch.canonical_hash,
                    universe=body.universe,
                    start_date=body.start,
                    end_date=body.end,
                    cutoff=body.decision_cutoff,
                )
                for baseline in baselines
            }
            for baseline in baselines:
                for simulator in body.simulators:
                    runs.append(
                        runner.execute(
                            RunCanonicalSecuritiesBaselineCommand(
                                campaign_id=body.campaign_id,
                                baseline_id=baseline.baseline_id,
                                data_batch_id=body.data_batch_id,
                                universe=tuple(body.universe),
                                start_date=body.start,
                                end_date=body.end,
                                cutoff=body.decision_cutoff,
                                simulator=simulator,
                                idempotency_key=(
                                    f"{idempotency_key}:{baseline.family.value}:{simulator.value}"
                                ),
                                started_at=datetime.now(tz=UTC),
                                completed_at=datetime.now(tz=UTC),
                            )
                        )
                    )
            comparer = CompareCanonicalSecuritiesSimulators(securities_baseline_repository)
            reports = []
            for baseline in baselines:
                scoped = [item for item in runs if item.baseline_id == baseline.baseline_id]
                reference = next(
                    (
                        item
                        for item in scoped
                        if item.simulator_identity is SecuritiesSimulatorIdentity.APEX_RESEARCH
                    ),
                    None,
                )
                if reference is None:
                    continue
                for comparison in scoped:
                    if comparison is not reference:
                        reports.append(
                            comparer.execute(
                                reference=reference,
                                comparison=comparison,
                            )
                        )
                if reference.state is SecuritiesSimulationState.COMPLETED:
                    conformance_state, integrity_warnings = scorecard_conformance(
                        tuple(
                            report
                            for report in reports
                            if report.baseline_id == baseline.baseline_id
                        )
                    )
                    scorecard = build_baseline_scorecard(
                        baseline=baseline,
                        request=canonical_requests[baseline.baseline_id],
                        run=reference,
                        conformance_state=conformance_state,
                        integrity_warnings=integrity_warnings,
                    )
                    securities_baseline_repository.save_scorecard(scorecard)
            return _securities_envelope(
                _baseline_payload(
                    {
                        "runs": runs,
                        "reports": reports,
                        "scorecards": securities_baseline_repository.scorecards(),
                        "trial_ledger": research_trial_summary(
                            securities_baseline_repository.runs(),
                            baselines,
                            securities_baseline_repository.reports(),
                        ),
                    }
                )
            )
        except SecuritiesBaselineIdempotencyConflict as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (
            SecuritiesBaselineApplicationError,
            SecuritiesApplicationError,
            ValueError,
        ) as error:
            raise _http_error(
                request, 422, "SECURITIES_BASELINE_SUITE_REJECTED", str(error)
            ) from error

    def securities_paper_payload(value: dict[str, object]) -> dict[str, object]:
        return {
            "warnings": (SECURITIES_PAPER_WARNING, NO_BROKER_WARNING),
            **value,
        }

    @app.get("/api/v1/securities/paper")
    def securities_paper_status(campaign_id: str) -> dict[str, object]:
        return _securities_paper_envelope(
            securities_paper_payload(securities_paper_repository.presentation(campaign_id))
        )

    @app.post(
        "/api/v1/securities/paper/runs",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_securities_paper_run(
        body: SecuritiesPaperRunRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            now = datetime.now(tz=UTC)
            trial = evidence_review_store.reconcile_trial(
                body.trial_id,
                now=now,
            )
            descriptor = evidence_review_store.descriptor(body.strategy_id)
            if not descriptor.last_evidence_decision:
                raise SecuritiesPaperApplicationError(
                    "governed PAPER authority decision is required"
                )
            batch = securities_repository.get_batch(body.data_batch_id)
            research_runs = tuple(
                item
                for item in securities_repository.list_research_runs(campaign_id=body.campaign_id)
                if item.data_batch_id == body.data_batch_id
                and securities_repository.get_specification(item.specification_id).strategy_id
                == body.strategy_id
            )
            if not research_runs:
                raise SecuritiesPaperApplicationError(
                    "no exact retained Securities research run matches the requested binding"
                )
            research_run = research_runs[-1]
            signals = tuple(
                item
                for item in securities_repository.list_signals(research_run.research_run_id)
                if item.direction is SecuritiesSignalDirection.LONG
                and body.start <= item.signal_at.date().isoformat() <= body.end
            )
            _instruments, _sessions, bars, _actions = securities_repository.get_dataset(
                batch.batch_id
            )
            by_instrument: dict[str, list[Any]] = defaultdict(list)
            for bar in bars:
                by_instrument[bar.instrument_id].append(bar)
            for retained_bars in by_instrument.values():
                retained_bars.sort(key=lambda item: item.period_start)
            policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=now)
            run = CreateSecuritiesPaperRun(
                repository=securities_paper_repository,
                campaigns=securities_paper_campaigns,
            ).execute(
                CreateSecuritiesPaperRunCommand(
                    campaign_id=body.campaign_id,
                    trial=trial,
                    descriptor=descriptor,
                    data_batch_id=batch.batch_id,
                    data_batch_hash=ContentHash(batch.canonical_hash),
                    policy=policy,
                    start_date=body.start,
                    end_date=body.end,
                    signal_count=len(signals),
                    idempotency_key=idempotency_key,
                    requested_at=now,
                )
            )
            if run.state is not SecuritiesPaperRunState.RUNNING:
                return _securities_paper_envelope(
                    securities_paper_payload(
                        {
                            "run": run,
                            "idempotent_replay": True,
                            **securities_paper_repository.presentation(body.campaign_id),
                        }
                    )
                )
            builder = BuildSecuritiesPaperOpportunities(securities_paper_repository)
            executor = RunSecuritiesPaperExecution(
                repository=securities_paper_repository,
                campaigns=securities_paper_campaigns,
            )
            instruments = {
                item.instrument_id: item for item in securities_repository.list_instruments()
            }
            execution_count = 0
            maximum = min(
                len(signals),
                max(
                    0,
                    trial.limits.maximum_approved_opportunities
                    - trial.usage.approved_opportunities,
                ),
                max(
                    0,
                    trial.limits.maximum_paper_orders - trial.usage.paper_orders,
                ),
            )
            for ordinal, signal in enumerate(signals[:maximum]):
                eligible = next(
                    (
                        item
                        for item in by_instrument.get(signal.instrument_id, ())
                        if item.period_start > signal.signal_at
                        and item.available_at > signal.signal_at
                    ),
                    None,
                )
                if eligible is None:
                    continue
                cash = securities_paper_campaigns.snapshot(body.campaign_id).settled_cash.amount
                raw_quantity = min(
                    policy.maximum_quantity,
                    cash * abs(signal.target_weight) / signal.reference_close,
                )
                quantity_value = (raw_quantity / policy.quantity_increment).to_integral_value(
                    rounding=ROUND_FLOOR
                ) * policy.quantity_increment
                if quantity_value < policy.minimum_quantity:
                    continue
                opportunity = builder.execute(
                    BuildSecuritiesPaperOpportunityCommand(
                        run_id=run.run_id,
                        signal=signal,
                        next_bar=eligible,
                        requested_quantity=Quantity(
                            quantity_value,
                            policy.quantity_increment,
                        ),
                        policy=policy,
                        holding_horizon_sessions=5,
                    )
                )
                current_trial = evidence_review_store.reconcile_trial(
                    trial.authorization_id,
                    now=datetime.now(tz=UTC),
                )
                if not current_trial.active_at(signal.signal_at):
                    break
                try:
                    executor.execute(
                        ExecuteSecuritiesPaperOpportunityCommand(
                            run_id=run.run_id,
                            opportunity=opportunity,
                            signal=signal,
                            next_bar=eligible,
                            policy=policy,
                            trial=current_trial,
                            strategy_authority_decision_id=(descriptor.last_evidence_decision),
                            symbol=instruments[signal.instrument_id].symbol,
                            idempotency_key=(f"{idempotency_key}:order:{ordinal}"),
                            expires_at=eligible.period_end,
                        )
                    )
                    execution_count += 1
                except SecuritiesPaperApplicationError as error:
                    if str(error) != "SECURITIES_OPPORTUNITY_REJECTED":
                        raise
            current = securities_paper_repository.run(run.run_id)
            has_open_positions = any(
                item.run_id == run.run_id and item.open_quantity.value > 0
                for item in securities_paper_repository.positions(body.campaign_id)
            )
            retained_run = (
                current
                if has_open_positions
                else complete_run(
                    current,
                    repository=securities_paper_repository,
                    completed_at=datetime.now(tz=UTC),
                )
            )
            return _securities_paper_envelope(
                securities_paper_payload(
                    {
                        "run": retained_run,
                        "execution_count": execution_count,
                        **securities_paper_repository.presentation(body.campaign_id),
                    }
                )
            )
        except SecuritiesPaperIdempotencyConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (
            SecuritiesApplicationError,
            SecuritiesPaperApplicationError,
            ValueError,
        ) as error:
            raise _http_error(request, 422, "SECURITIES_PAPER_RUN_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/securities/paper/campaigns/{campaign_id}/marks",
        dependencies=[Depends(_mutation_guard)],
    )
    def mark_securities_paper_positions(
        campaign_id: str,
        body: SecuritiesPaperAsOfRequest,
        request: Request,
        _idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=body.as_of)
            service = MarkSecuritiesPositions(securities_paper_repository)
            marks = []
            for position in securities_paper_repository.positions(campaign_id):
                if position.open_quantity.value == 0:
                    continue
                run = securities_paper_repository.run(position.run_id)
                _instruments, _sessions, bars, _actions = securities_repository.get_dataset(
                    run.data_batch_id
                )
                eligible = tuple(
                    item
                    for item in bars
                    if item.instrument_id == position.instrument_id
                    and item.available_at <= body.as_of
                    and (position.last_mark_at is None or item.available_at > position.last_mark_at)
                )
                if eligible:
                    marks.append(
                        service.execute(
                            position_id=position.position_id,
                            bar=max(
                                eligible,
                                key=lambda item: item.available_at,
                            ),
                            policy=policy,
                            as_of=body.as_of,
                        )
                    )
            return _securities_paper_envelope(
                securities_paper_payload(
                    {
                        "applied_marks": marks,
                        **securities_paper_repository.presentation(campaign_id),
                    }
                )
            )
        except (SecuritiesApplicationError, SecuritiesPaperApplicationError, ValueError) as error:
            raise _http_error(request, 422, "SECURITIES_MARK_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/securities/paper/campaigns/{campaign_id}/corporate-actions",
        dependencies=[Depends(_mutation_guard)],
    )
    def apply_securities_paper_actions(
        campaign_id: str,
        body: SecuritiesPaperAsOfRequest,
        request: Request,
        _idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            service = ApplySecuritiesCorporateActions(securities_paper_repository)
            applications = []
            for position in securities_paper_repository.positions(campaign_id):
                if position.open_quantity.value == 0:
                    continue
                run = securities_paper_repository.run(position.run_id)
                _instruments, _sessions, _bars, actions = securities_repository.get_dataset(
                    run.data_batch_id
                )
                for action in actions:
                    if (
                        action.instrument_id == position.instrument_id
                        and action.available_at <= body.as_of
                    ):
                        applications.append(
                            service.execute(
                                position_id=position.position_id,
                                action=action,
                                applied_at=body.as_of,
                                quantity_increment=(position.open_quantity.increment),
                            )
                        )
            return _securities_paper_envelope(
                securities_paper_payload(
                    {
                        "applications": applications,
                        **securities_paper_repository.presentation(campaign_id),
                    }
                )
            )
        except (SecuritiesApplicationError, SecuritiesPaperApplicationError, ValueError) as error:
            raise _http_error(
                request,
                422,
                "SECURITIES_CORPORATE_ACTION_REJECTED",
                str(error),
            ) from error

    @app.post(
        "/api/v1/securities/paper/positions/{position_id}/exit",
        dependencies=[Depends(_mutation_guard)],
    )
    def exit_securities_paper_position(
        position_id: str,
        body: SecuritiesPaperExitRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            position = securities_paper_repository.position(position_id)
            if position.open_quantity.value == 0:
                raise SecuritiesPaperApplicationError("Securities Position is already closed")
            run = securities_paper_repository.run(position.run_id)
            _instruments, _sessions, bars, _actions = securities_repository.get_dataset(
                run.data_batch_id
            )
            next_bar = next(
                (
                    item
                    for item in sorted(
                        bars,
                        key=lambda item: item.period_start,
                    )
                    if item.instrument_id == position.instrument_id
                    and item.period_start > body.as_of
                ),
                None,
            )
            if next_bar is None:
                raise SecuritiesPaperApplicationError("no later eligible retained bar exists")
            result = ExitSecuritiesPositions(securities_paper_repository).execute(
                ExitSecuritiesPositionCommand(
                    position_id=position.position_id,
                    reason=body.reason,
                    decision_at=body.as_of,
                    next_bar=next_bar,
                    requested_quantity=Quantity(
                        position.open_quantity.value,
                        position.open_quantity.increment,
                    ),
                    policy=SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=body.as_of),
                    source_identity=f"operator:{idempotency_key}",
                    idempotency_key=idempotency_key,
                    expires_at=next_bar.period_end,
                )
            )
            current = securities_paper_repository.run(run.run_id)
            if current.state is SecuritiesPaperRunState.RUNNING and not any(
                item.run_id == run.run_id and item.open_quantity.value > 0
                for item in securities_paper_repository.positions(position.campaign_id)
            ):
                complete_run(
                    current,
                    repository=securities_paper_repository,
                    completed_at=datetime.now(tz=UTC),
                )
            return _securities_paper_envelope(
                securities_paper_payload(
                    {
                        "exit": result,
                        **securities_paper_repository.presentation(position.campaign_id),
                    }
                )
            )
        except (SecuritiesApplicationError, SecuritiesPaperApplicationError, ValueError) as error:
            raise _http_error(request, 422, "SECURITIES_EXIT_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/securities/paper/conformance",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_securities_paper_conformance(
        body: SecuritiesPaperConformanceRequest,
        request: Request,
        _idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            run = securities_paper_repository.run(body.paper_run_id)
            presentation = securities_paper_repository.presentation(run.campaign_id)
            order_ids = tuple(
                str(item["order_id"])
                for item in cast(list[dict[str, object]], presentation["orders"])
                if item["run_id"] == run.run_id
            )
            fill_ids = tuple(
                str(item["fill_id"])
                for item in cast(list[dict[str, object]], presentation["fills"])
                if item["run_id"] == run.run_id and Decimal(str(item["quantity"])) > 0
            )
            events = tuple(("ORDER", item) for item in order_ids) + tuple(
                ("FILL", item) for item in fill_ids
            )
            external = lean_securities_paper.unavailable_run(
                apex_run=run,
                request_hash=run.canonical_input_hash,
            )
            report = BuildSecuritiesPaperConformance(securities_paper_repository).execute(
                run=run,
                apex_events=events,
                external=external,
            )
            return _securities_paper_envelope(
                securities_paper_payload(
                    {
                        "external_warning": EXTERNAL_ENGINE_WARNING,
                        "requested_engine": body.external_engine,
                        "external_run": external,
                        "report": report,
                    }
                )
            )
        except (SecuritiesApplicationError, SecuritiesPaperApplicationError, ValueError) as error:
            raise _http_error(
                request, 422, "SECURITIES_PAPER_CONFORMANCE_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/strategy-review-policies")
    def strategy_review_policies() -> dict[str, object]:
        ensure_evidence_review_policies()
        return _envelope({"items": evidence_review_store.policies()})

    @app.get("/api/v1/strategy-review-policies/{policy_id}")
    def strategy_review_policy(
        policy_id: str, request: Request, version: str = "apex-evidence-review-policy-v1"
    ) -> dict[str, object]:
        try:
            ensure_evidence_review_policies()
            return _envelope(evidence_review_store.policy(policy_id, version))
        except EvidenceReviewError as error:
            raise _http_error(request, 404, "REVIEW_POLICY_NOT_FOUND", str(error)) from error

    @app.post(
        "/api/v1/strategies/{strategy_id}/evidence-snapshots",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def build_evidence_snapshot(
        strategy_id: str,
        body: EvidenceSnapshotRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            ensure_evidence_review_policies()
            descriptor = evidence_review_store.descriptor(strategy_id)
            snapshot, criteria = BuildStrategyEvidenceSnapshot(evidence_review_store).build(
                BuildStrategyEvidenceSnapshotCommand(
                    strategy_id=strategy_id,
                    strategy_version=descriptor.version,
                    configuration_hash=ContentHash(body.configuration_hash),
                    policy_id=body.policy_id,
                    policy_version=body.policy_version,
                    cutoff_at=body.cutoff_at,
                    full_history_started_at=body.full_history_started_at,
                    recent_history_started_at=body.recent_history_started_at,
                    idempotency_key=idempotency_key,
                )
            )
            return _envelope({"snapshot": snapshot, "criteria": criteria})
        except EvidenceReviewConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (EvidenceReviewError, ValueValidationError) as error:
            raise _http_error(request, 422, "EVIDENCE_SNAPSHOT_REJECTED", str(error)) from error

    @app.get("/api/v1/strategies/{strategy_id}/evidence-snapshots")
    def strategy_evidence_snapshots(strategy_id: str) -> dict[str, object]:
        return _envelope({"items": evidence_review_store.snapshots_for(strategy_id)})

    @app.get("/api/v1/evidence-snapshots/{snapshot_id}")
    def evidence_snapshot(snapshot_id: str, request: Request) -> dict[str, object]:
        try:
            return _envelope(evidence_review_store.snapshot(snapshot_id))
        except EvidenceReviewError as error:
            raise _http_error(request, 404, "EVIDENCE_SNAPSHOT_NOT_FOUND", str(error)) from error

    @app.get("/api/v1/evidence-snapshots/{snapshot_id}/criteria")
    def evidence_snapshot_criteria(snapshot_id: str, request: Request) -> dict[str, object]:
        try:
            evidence_review_store.snapshot(snapshot_id)
            return _envelope({"items": evidence_review_store.criteria(snapshot_id)})
        except EvidenceReviewError as error:
            raise _http_error(request, 404, "EVIDENCE_SNAPSHOT_NOT_FOUND", str(error)) from error

    @app.post(
        "/api/v1/strategies/{strategy_id}/reviews",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def request_strategy_review(
        strategy_id: str,
        body: EvidenceReviewRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            descriptor = evidence_review_store.descriptor(strategy_id)
            review = RequestStrategyEvidenceReview(evidence_review_store).request(
                RequestStrategyEvidenceReviewCommand(
                    strategy_id=strategy_id,
                    strategy_version=descriptor.version,
                    snapshot_id=body.snapshot_id,
                    campaign_id=body.campaign_id,
                    expires_at=body.expires_at,
                    operator_reason=body.operator_reason,
                    operator_note=body.operator_note,
                    idempotency_key=idempotency_key,
                ),
                now=datetime.now(tz=UTC),
            )
            return _envelope(review)
        except EvidenceReviewConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (EvidenceReviewError, ValueValidationError) as error:
            raise _http_error(request, 422, "EVIDENCE_REVIEW_REJECTED", str(error)) from error

    @app.get("/api/v1/strategies/{strategy_id}/reviews")
    def strategy_reviews(strategy_id: str) -> dict[str, object]:
        return _envelope({"items": evidence_review_store.reviews_for(strategy_id)})

    @app.get("/api/v1/strategy-reviews/{review_id}")
    def strategy_review(review_id: str, request: Request) -> dict[str, object]:
        try:
            return _envelope(evidence_review_store.review(review_id))
        except EvidenceReviewError as error:
            raise _http_error(request, 404, "EVIDENCE_REVIEW_NOT_FOUND", str(error)) from error

    @app.post("/api/v1/strategy-reviews/{review_id}/start", dependencies=[Depends(_mutation_guard)])
    def start_strategy_review(
        review_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            return _envelope(
                StartStrategyEvidenceReview(evidence_review_store).start(
                    review_id=review_id,
                    operator_reason=body.operator_reason,
                    operator_note=body.operator_note,
                    idempotency_key=idempotency_key,
                    now=datetime.now(tz=UTC),
                )
            )
        except EvidenceReviewError as error:
            raise _http_error(request, 422, "EVIDENCE_REVIEW_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-reviews/{review_id}/approve", dependencies=[Depends(_mutation_guard)]
    )
    def approve_strategy_review(
        review_id: str,
        body: PaperTrialApprovalRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        try:
            review, trial = DecideStrategyEvidenceReview(evidence_review_store).decide(
                DecideStrategyEvidenceReviewCommand(
                    review_id=review_id,
                    decision=StrategyEvidenceReviewState.APPROVED,
                    operator_reason=body.operator_reason,
                    operator_note=body.operator_note,
                    idempotency_key=idempotency_key,
                    confirmation_text=body.confirmation_text,
                ),
                now=datetime.now(tz=UTC),
                trial_limits=PaperTrialLimits(
                    paper_execution_policy_id=body.paper_execution_policy_id,
                    paper_execution_policy_hash=ContentHash(body.paper_execution_policy_hash),
                    starts_at=body.starts_at,
                    expires_at=body.expires_at,
                    maximum_paper_cycles=body.maximum_paper_cycles,
                    maximum_approved_opportunities=body.maximum_approved_opportunities,
                    maximum_paper_orders=body.maximum_paper_orders,
                    maximum_simultaneous_open_positions=body.maximum_simultaneous_open_positions,
                    maximum_per_opportunity_risk_fraction=RiskFraction(
                        body.maximum_per_opportunity_risk_fraction
                    ),
                    maximum_strategy_active_risk_fraction=RiskFraction(
                        body.maximum_strategy_active_risk_fraction
                    ),
                    maximum_cumulative_authorized_risk=Money(
                        body.maximum_cumulative_authorized_risk
                    ),
                    maximum_cumulative_realized_paper_loss=Money(
                        body.maximum_cumulative_realized_paper_loss
                    ),
                    maximum_paper_drawdown=Money(body.maximum_paper_drawdown),
                    maximum_capital_lockup_seconds=body.maximum_capital_lockup_seconds,
                    allowed_instrument_class=body.allowed_instrument_class,
                    allowed_provider=body.allowed_provider,
                ),
            )
            return _envelope({"review": review, "paper_trial": trial})
        except EvidenceReviewConflictError as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        except (EvidenceReviewError, ValueValidationError) as error:
            raise _http_error(request, 422, "PAPER_TRIAL_APPROVAL_REJECTED", str(error)) from error

    def _review_transition(
        review_id: str,
        state: StrategyEvidenceReviewState,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            review = evidence_review_store.review(review_id)
            result, _ = evidence_review_store.transition_review(
                review,
                state=state,
                reason_codes=(f"OPERATOR_{state.value}",),
                operator_reason=body.operator_reason,
                operator_note=body.operator_note,
                idempotency_key=idempotency_key,
                now=datetime.now(tz=UTC),
            )
            return _envelope(result)
        except EvidenceReviewError as error:
            raise _http_error(request, 422, "EVIDENCE_REVIEW_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/strategy-reviews/{review_id}/reject", dependencies=[Depends(_mutation_guard)]
    )
    def reject_strategy_review(
        review_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _review_transition(
            review_id, StrategyEvidenceReviewState.REJECTED, body, request, idempotency_key
        )

    @app.post("/api/v1/strategy-reviews/{review_id}/defer", dependencies=[Depends(_mutation_guard)])
    def defer_strategy_review(
        review_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _review_transition(
            review_id, StrategyEvidenceReviewState.DEFERRED, body, request, idempotency_key
        )

    @app.post(
        "/api/v1/strategy-reviews/{review_id}/cancel", dependencies=[Depends(_mutation_guard)]
    )
    def cancel_strategy_review(
        review_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _review_transition(
            review_id, StrategyEvidenceReviewState.CANCELED, body, request, idempotency_key
        )

    @app.get("/api/v1/campaigns/{campaign_id}/paper-trials")
    def campaign_paper_trials(campaign_id: str) -> dict[str, object]:
        return _envelope({"items": evidence_review_store.trials_for(campaign_id)})

    @app.get("/api/v1/paper-trials/{authorization_id}")
    def paper_trial(authorization_id: str, request: Request) -> dict[str, object]:
        try:
            return _envelope(evidence_review_store.trial(authorization_id))
        except EvidenceReviewError as error:
            raise _http_error(request, 404, "PAPER_TRIAL_NOT_FOUND", str(error)) from error

    def _trial_transition(
        authorization_id: str,
        state: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: str,
    ) -> dict[str, object]:
        from apex.application.evidence_review import PaperTrialAuthorizationState

        try:
            result = evidence_review_store.transition_trial(
                evidence_review_store.trial(authorization_id),
                state=PaperTrialAuthorizationState(state),
                reason=body.operator_reason,
                idempotency_key=idempotency_key,
                now=datetime.now(tz=UTC),
            )
            return _envelope(result)
        except EvidenceReviewError as error:
            raise _http_error(request, 422, "PAPER_TRIAL_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/paper-trials/{authorization_id}/pause", dependencies=[Depends(_mutation_guard)]
    )
    def pause_paper_trial(
        authorization_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _trial_transition(authorization_id, "PAUSED", body, request, idempotency_key)

    @app.post(
        "/api/v1/paper-trials/{authorization_id}/resume", dependencies=[Depends(_mutation_guard)]
    )
    def resume_paper_trial(
        authorization_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _trial_transition(authorization_id, "ACTIVE", body, request, idempotency_key)

    @app.post(
        "/api/v1/paper-trials/{authorization_id}/revoke", dependencies=[Depends(_mutation_guard)]
    )
    def revoke_paper_trial(
        authorization_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _trial_transition(authorization_id, "REVOKED", body, request, idempotency_key)

    @app.post(
        "/api/v1/paper-trials/{authorization_id}/complete", dependencies=[Depends(_mutation_guard)]
    )
    def complete_paper_trial(
        authorization_id: str,
        body: EvidenceReviewTransitionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _trial_transition(authorization_id, "COMPLETED", body, request, idempotency_key)

    @app.get("/api/v1/overview")
    def overview() -> dict[str, object]:
        return _envelope(store.overview())

    @app.get("/api/v1/campaigns")
    def campaigns(request: Request, limit: int = 50, offset: int = 0) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope(
            {
                "items": store.list_campaigns(limit=limit, offset=offset),
                "offset": offset,
                "limit": limit,
            }
        )

    @app.post(
        "/api/v1/campaigns",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_campaign(
        body: CampaignCreateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        campaign_id = deterministic_id(
            "operator_campaign", {"idempotency_key": idempotency_key, "name": body.name}
        )
        try:
            result = CreateMoonshotCampaign(store).create(
                CreateMoonshotCampaignCommand(
                    campaign_id=campaign_id,
                    name=body.name,
                    initial_capital=Money(body.initial_capital),
                    maximum_authorized_loss=Money(body.maximum_authorized_loss),
                    starts_at=body.starts_at,
                    expires_at=body.expires_at,
                    operating_mode=body.operating_mode,
                    maximum_active_risk_fraction=RiskFraction(body.maximum_active_risk_fraction),
                    maximum_per_opportunity_risk_fraction=RiskFraction(
                        body.maximum_per_opportunity_risk_fraction
                    ),
                    maximum_per_strategy_risk_fraction=RiskFraction(
                        body.maximum_per_strategy_risk_fraction
                    ),
                    maximum_per_correlation_bucket_risk_fraction=RiskFraction(
                        body.maximum_per_correlation_bucket_risk_fraction
                    ),
                    maximum_simultaneous_reservations=body.maximum_simultaneous_reservations,
                    maximum_simultaneous_positions=body.maximum_simultaneous_positions,
                    maximum_capital_lockup_seconds=body.maximum_capital_lockup_seconds,
                    minimum_conservative_expected_value=Money(
                        body.minimum_conservative_expected_value
                    ),
                    minimum_evidence_tier=body.minimum_evidence_tier,
                    allow_profit_compounding=body.allow_profit_compounding,
                    operator_note=body.operator_note,
                    idempotency_key=idempotency_key,
                )
            )
        except (ValueValidationError, ValueError) as error:
            raise _http_error(request, 422, "CAMPAIGN_POLICY_REJECTED", str(error)) from error
        except (IdempotencyConflictError, PersistenceConflictError) as error:
            raise _http_error(request, 409, "IDEMPOTENCY_CONFLICT", str(error)) from error
        return _envelope(result)

    @app.get("/api/v1/campaigns/{campaign_id}")
    def campaign(campaign_id: str, request: Request) -> dict[str, object]:
        value = store.campaign_detail(campaign_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CAMPAIGN_NOT_FOUND",
                "campaign was not found",
                entity_type="campaign",
                entity_id=campaign_id,
            )
        return _envelope(value)

    @app.get("/api/v1/campaigns/{campaign_id}/status")
    def campaign_status(campaign_id: str, request: Request) -> dict[str, object]:
        value = store.campaign_detail(campaign_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CAMPAIGN_NOT_FOUND",
                "campaign was not found",
                entity_type="campaign",
                entity_id=campaign_id,
            )
        return _envelope(value)

    def _control(
        campaign_id: str, action: str, body: ControlRequest, request: Request, idempotency_key: str
    ) -> dict[str, object]:
        if action == "KILL" and body.confirmation != campaign_id:
            raise _http_error(
                request,
                422,
                "KILL_CONFIRMATION_REQUIRED",
                "kill confirmation must exactly match the Campaign ID",
            )
        try:
            from apex.cli import autopilot_control

            result = autopilot_control(
                campaign_id=campaign_id,
                action=action,
                reason=body.operator_reason,
                idempotency_key=idempotency_key,
            )
            store.record_action(
                campaign_id=campaign_id,
                action_type=OperatorActionType(f"{action}_CAMPAIGN"),
                idempotency_key=idempotency_key,
                operator_note=body.operator_reason,
                target_entity_ids=(campaign_id,),
                reason_codes=(
                    {
                        "PAUSE": "CAMPAIGN_PAUSED",
                        "RESUME": "CAMPAIGN_RESUMED",
                        "KILL": "CAMPAIGN_KILLED",
                    }[action],
                ),
                request=body.model_dump(),
                result=result,
                confirmation_text=body.confirmation,
            )
            return _envelope(result)
        except (CampaignControlError, AutopilotError) as error:
            raise _http_error(
                request,
                423,
                "CAMPAIGN_CONTROL_REJECTED",
                str(error),
                entity_type="campaign",
                entity_id=campaign_id,
            ) from error

    @app.post(
        "/api/v1/campaigns/{campaign_id}/pause",
        dependencies=[Depends(_mutation_guard)],
    )
    def pause(
        campaign_id: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _control(campaign_id, "PAUSE", body, request, idempotency_key)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/resume",
        dependencies=[Depends(_mutation_guard)],
    )
    def resume(
        campaign_id: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _control(campaign_id, "RESUME", body, request, idempotency_key)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/kill",
        dependencies=[Depends(_mutation_guard)],
    )
    def kill(
        campaign_id: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _control(campaign_id, "KILL", body, request, idempotency_key)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/cycles",
        dependencies=[Depends(_mutation_guard)],
    )
    def cycle(
        campaign_id: str,
        body: CycleRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        metadata_value = store.campaign_metadata(campaign_id)
        if metadata_value is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        if str(metadata_value["operating_mode"]) != body.mode.value:
            raise _http_error(
                request,
                422,
                "OPERATING_MODE_MISMATCH",
                "a Campaign can run only its retained SHADOW or PAPER operating mode",
                entity_type="campaign",
                entity_id=campaign_id,
            )
        try:
            from apex.cli import autopilot_cycle

            result = autopilot_cycle(
                campaign_id=campaign_id,
                mode=body.mode.value.lower(),
                idempotency_key=idempotency_key,
                maximum_strategies=body.maximum_strategies,
                maximum_proposals=body.maximum_proposals,
                deadline_seconds=30,
                operator_note=body.operator_note,
            )
            action_type = (
                OperatorActionType.RUN_PAPER_CYCLE
                if body.mode is OperatorMode.PAPER
                else OperatorActionType.RUN_SHADOW_CYCLE
            )
            store.record_action(
                campaign_id=campaign_id,
                action_type=action_type,
                idempotency_key=idempotency_key,
                operator_note=body.operator_note,
                target_entity_ids=(campaign_id, result.cycle_id),
                reason_codes=("CYCLE_COMPLETED",),
                request=body.model_dump(),
                result=result,
            )
            return _envelope(result)
        except CampaignControlError as error:
            raise _http_error(
                request,
                423,
                "CAMPAIGN_NOT_RUNNABLE",
                str(error),
                entity_type="campaign",
                entity_id=campaign_id,
            ) from error
        except AutopilotError as error:
            raise _http_error(request, 422, "CYCLE_REJECTED", str(error)) from error

    @app.get("/api/v1/campaigns/{campaign_id}/cycles")
    def campaign_cycles(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope({"items": store.cycles(campaign_id, limit=limit, offset=offset)})

    @app.get("/api/v1/cycles/{cycle_id}")
    def cycle_detail(cycle_id: str, request: Request) -> dict[str, object]:
        value = store.cycle(cycle_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "CYCLE_NOT_FOUND",
                "cycle was not found",
                entity_type="cycle",
                entity_id=cycle_id,
            )
        return _envelope(value)

    @app.get("/api/v1/cycles/{cycle_id}/invocations")
    def cycle_invocations(cycle_id: str) -> dict[str, object]:
        return _envelope({"items": store.invocations(cycle_id)})

    @app.get("/api/v1/cycles/{cycle_id}/opportunities")
    def cycle_opportunities(cycle_id: str) -> dict[str, object]:
        return _envelope({"items": store.opportunities(cycle_id=cycle_id)})

    @app.get("/api/v1/campaigns/{campaign_id}/opportunities")
    def campaign_opportunities(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope(
            {"items": store.opportunities(campaign_id=campaign_id, limit=limit, offset=offset)}
        )

    @app.get("/api/v1/opportunities/{opportunity_id}")
    def opportunity(opportunity_id: str, request: Request) -> dict[str, object]:
        value = store.opportunity(opportunity_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "OPPORTUNITY_NOT_FOUND",
                "opportunity was not found",
                entity_type="opportunity",
                entity_id=opportunity_id,
            )
        return _envelope(value)

    @app.get("/api/v1/strategies")
    def strategies() -> dict[str, object]:
        from apex.adapters.postgres import postgres_strategy_shadow_store
        from apex.cli import autopilot_strategies

        shadow_store = postgres_strategy_shadow_store(engine)
        items: list[object] = []
        for item in autopilot_strategies():
            if not isinstance(item, dict):
                items.append(item)
                continue
            value = dict(item)
            try:
                authority = shadow_store.current_authority(str(value["strategy_id"]))
                value["authority"] = authority["authority"]
                value["runnable"] = str(authority["authority"]) == "SHADOW"
            except AutopilotError:
                pass
            items.append(value)
        return _envelope({"items": items})

    @app.get("/api/v1/strategies/{strategy_id}")
    def strategy(strategy_id: str, request: Request) -> dict[str, object]:
        from apex.adapters.postgres import postgres_strategy_shadow_store
        from apex.cli import autopilot_strategies

        value = next(
            (
                item
                for item in autopilot_strategies()
                if isinstance(item, dict) and item.get("strategy_id") == strategy_id
            ),
            None,
        )
        if value is None:
            raise _http_error(
                request,
                404,
                "STRATEGY_NOT_FOUND",
                "strategy was not found",
                entity_type="strategy",
                entity_id=strategy_id,
            )
        try:
            authority = postgres_strategy_shadow_store(engine).current_authority(strategy_id)
            value = dict(value)
            value["authority"] = authority["authority"]
            value["runnable"] = str(authority["authority"]) == "SHADOW"
        except AutopilotError:
            pass
        return _envelope(value)

    @app.get("/api/v1/strategies/{strategy_id}/scorecard")
    def scorecard(strategy_id: str, campaign_id: str | None = None) -> dict[str, object]:
        from apex.adapters.postgres import postgres_strategy_shadow_store

        scorecards = (
            [
                item
                for item in store.strategy_scorecards(campaign_id)
                if str(item["strategy_id"]) == strategy_id
            ]
            if campaign_id is not None
            else []
        )
        shadow_scorecard: object | None = None
        try:
            shadow_scorecard = postgres_strategy_shadow_store(engine).scorecard(
                strategy_id=strategy_id, campaign_id=campaign_id
            )
        except AutopilotError:
            # Existing fixture-only scorecards remain a valid Console view even
            # when a strategy is not in the production shadow catalog.
            shadow_scorecard = None
        return _envelope(
            {
                "strategy_id": strategy_id,
                "campaign_id": campaign_id,
                "scorecards": scorecards,
                "shadow_scorecard": shadow_scorecard,
            }
        )

    @app.get("/api/v1/strategies/{strategy_id}/microstructure-features")
    def microstructure_features(
        strategy_id: str, campaign_id: str | None = None, limit: int = 100
    ) -> dict[str, object]:
        if strategy_id != "kalshi-orderbook-microstructure-v1":
            return _envelope({"strategy_id": strategy_id, "items": ()})
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="feature limit must be between one and 100")
        from apex.adapters.postgres import postgres_strategy_shadow_store

        return _envelope(
            {
                "strategy_id": strategy_id,
                "campaign_id": campaign_id,
                "items": postgres_strategy_shadow_store(engine).microstructure_features(
                    campaign_id=campaign_id, limit=limit
                ),
            }
        )

    @app.get("/api/v1/strategies/{strategy_id}/market-dynamics-features")
    def market_dynamics_features(
        strategy_id: str, campaign_id: str | None = None, limit: int = 100
    ) -> dict[str, object]:
        if strategy_id != "kalshi-market-dynamics-v1":
            return _envelope({"strategy_id": strategy_id, "items": ()})
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="feature limit must be between one and 100")
        from apex.adapters.postgres import postgres_strategy_shadow_store

        return _envelope(
            {
                "strategy_id": strategy_id,
                "campaign_id": campaign_id,
                "items": postgres_strategy_shadow_store(engine).market_dynamics_features(
                    campaign_id=campaign_id, limit=limit
                ),
            }
        )

    @app.get("/api/v1/strategy-comparisons")
    def strategy_comparisons(campaign_id: str | None = None, limit: int = 100) -> dict[str, object]:
        if not 1 <= limit <= 100:
            raise HTTPException(
                status_code=422, detail="comparison limit must be between one and 100"
            )
        from apex.adapters.postgres import postgres_strategy_shadow_store

        return _envelope(
            {
                "campaign_id": campaign_id,
                "items": postgres_strategy_shadow_store(engine).cross_strategy_shadow_comparisons(
                    campaign_id=campaign_id, limit=limit
                ),
            }
        )

    @app.get("/api/v1/strategies/{strategy_id}/authority-history")
    def strategy_authority_history_endpoint(
        strategy_id: str, request: Request
    ) -> dict[str, object]:
        from apex.cli import strategy_authority_history

        try:
            return _envelope({"items": strategy_authority_history(strategy_id)})
        except AutopilotError as error:
            raise _http_error(
                request,
                404,
                "STRATEGY_NOT_FOUND",
                str(error),
                entity_type="strategy",
                entity_id=strategy_id,
            ) from error

    @app.get("/api/v1/strategies/{strategy_id}/promotion-readiness")
    def strategy_promotion_readiness_endpoint(
        strategy_id: str, campaign_id: str | None = None
    ) -> dict[str, object]:
        from apex.cli import strategy_promotion_readiness

        try:
            return _envelope(
                strategy_promotion_readiness(strategy_id=strategy_id, campaign_id=campaign_id)
            )
        except AutopilotError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/api/v1/strategies/{strategy_id}/authority",
        dependencies=[Depends(_mutation_guard)],
    )
    def change_strategy_authority_endpoint(
        strategy_id: str,
        body: StrategyAuthorityRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        from apex.cli import change_strategy_authority

        try:
            return _envelope(
                change_strategy_authority(
                    strategy_id=strategy_id,
                    requested_authority=body.requested_authority,
                    operator_reason=body.operator_reason,
                    evidence_review_id=body.evidence_review_id,
                    idempotency_key=idempotency_key,
                    confirmation_text=body.confirmation_text,
                )
            )
        except (AutopilotError, ValueError, ValueValidationError) as error:
            raise _http_error(
                request,
                422,
                "AUTHORITY_TRANSITION_REJECTED",
                str(error),
                entity_type="strategy",
                entity_id=strategy_id,
            ) from error

    @app.post(
        "/api/v1/campaigns/{campaign_id}/strategies/kalshi-orderbook-microstructure-v1/shadow-observations",
        dependencies=[Depends(_mutation_guard)],
    )
    def run_microstructure_live_shadow(
        campaign_id: str,
        body: LiveShadowSessionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        """One manually triggered, sequential public-GET-only foreground session."""

        from apex.cli import live_shadow_session

        _ = idempotency_key  # the durable session records its own deterministic identity
        try:
            return _envelope(
                live_shadow_session(
                    campaign_id=campaign_id,
                    strategy_id="kalshi-orderbook-microstructure-v1",
                    iterations=body.iterations,
                    interval_seconds=body.interval_seconds,
                    market_limit=body.market_limit,
                    maximum_pages=body.maximum_pages,
                    maximum_enrichments=body.maximum_enrichments,
                    observations_per_market=body.observations_per_market,
                    orderbook_depth=body.orderbook_depth,
                )
            )
        except (AutopilotError, CampaignControlError, ValueError) as error:
            raise _http_error(request, 422, "LIVE_SHADOW_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/campaigns/{campaign_id}/strategies/kalshi-market-dynamics-v1/shadow-observations",
        dependencies=[Depends(_mutation_guard)],
    )
    def run_market_dynamics_shadow(
        campaign_id: str,
        body: LiveShadowSessionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        """Manual foreground public-market observation; no order endpoint is reachable."""

        from apex.cli import live_shadow_session

        _ = idempotency_key
        try:
            return _envelope(
                live_shadow_session(
                    campaign_id=campaign_id,
                    strategy_id="kalshi-market-dynamics-v1",
                    iterations=body.iterations,
                    interval_seconds=body.interval_seconds,
                    market_limit=body.market_limit,
                    maximum_pages=body.maximum_pages,
                    maximum_enrichments=body.maximum_enrichments,
                    observations_per_market=body.observations_per_market,
                    orderbook_depth=body.orderbook_depth,
                )
            )
        except (AutopilotError, CampaignControlError, ValueError) as error:
            raise _http_error(request, 422, "LIVE_SHADOW_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/campaigns/{campaign_id}/multi-strategy-shadow-session",
        dependencies=[Depends(_mutation_guard)],
    )
    def run_multi_strategy_shadow_session(
        campaign_id: str,
        body: MultiStrategyShadowSessionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        """Run one foreground public-GET-only campaign comparison session."""

        from apex.cli import multi_strategy_shadow_session

        _ = idempotency_key
        try:
            return _envelope(
                multi_strategy_shadow_session(
                    campaign_id=campaign_id,
                    iterations=body.iterations,
                    interval_seconds=body.interval_seconds,
                    market_limit=body.market_limit,
                    maximum_pages=body.maximum_pages,
                    maximum_enrichments=body.maximum_enrichments,
                    orderbook_depth=body.orderbook_depth,
                )
            )
        except (AutopilotError, CampaignControlError, ValueError) as error:
            raise _http_error(request, 422, "MULTI_STRATEGY_SHADOW_REJECTED", str(error)) from error

    @app.get("/api/v1/campaigns/{campaign_id}/multi-strategy-cycles")
    def multi_strategy_cycles(campaign_id: str, request: Request) -> dict[str, object]:
        from apex.adapters.postgres import postgres_multi_strategy_shadow_store

        if store.campaign_metadata(campaign_id) is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope(
            {"items": postgres_multi_strategy_shadow_store(engine).cycles(campaign_id)}
        )

    @app.get("/api/v1/multi-strategy-cycles/{cycle_id}")
    def multi_strategy_cycle(cycle_id: str, request: Request) -> dict[str, object]:
        from apex.adapters.postgres import postgres_multi_strategy_shadow_store

        value = postgres_multi_strategy_shadow_store(engine).cycle_detail(cycle_id)
        if value is None:
            raise _http_error(request, 404, "MULTI_STRATEGY_CYCLE_NOT_FOUND", "cycle was not found")
        return _envelope(value)

    @app.get("/api/v1/campaigns/{campaign_id}/shadow-plans")
    def shadow_plans(campaign_id: str, request: Request, limit: int = 50) -> dict[str, object]:
        _page(request, limit, 0)
        if store.campaign_metadata(campaign_id) is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope(
            {"items": shadow_operations_store.plans(campaign_id=campaign_id, limit=limit)}
        )

    @app.post(
        "/api/v1/campaigns/{campaign_id}/shadow-plans/discover-and-create",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def discover_create_shadow_evidence_plan_endpoint(
        campaign_id: str,
        body: AtomicShadowEvidencePlanRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        """Run the same finite public-only atomic cohort command as the CLI."""

        from apex.application.capture_public_orderbook_session import PublicOrderbookCaptureError
        from apex.application.discover_public_markets import PublicMarketDiscoveryError
        from apex.cli import discover_create_shadow_plan

        try:
            value = discover_create_shadow_plan(
                campaign_id=campaign_id,
                name=body.name,
                window_start=body.window_start,
                window_end=body.window_end,
                iterations=body.iterations,
                interval_seconds=body.interval_seconds,
                target_markets=body.target_markets,
                minimum_markets=body.minimum_markets,
                minimum_events=body.minimum_events,
                maximum_pages=body.maximum_pages,
                maximum_inspected_markets=body.maximum_inspected_markets,
                maximum_enrichments=body.maximum_enrichments,
                maximum_markets_per_event=body.maximum_markets_per_event,
                maximum_markets_per_series=body.maximum_markets_per_series,
                orderbook_depth=body.orderbook_depth,
                idempotency_key=idempotency_key,
                maximum_orderbook_preflights=body.maximum_orderbook_preflights,
                microstructure_configuration_version=body.microstructure_configuration_version,
                prior_plan_ids=tuple(body.exclude_plan_ids),
                exclude_prior_plan_markets=body.exclude_prior_plan_markets,
                exclude_prior_plan_events=body.exclude_prior_plan_events,
                exclude_active_plan_markets=body.exclude_active_plan_markets,
            )
            return _envelope(value)
        except (
            AutopilotError,
            CampaignControlError,
            PublicMarketDiscoveryError,
            PublicOrderbookCaptureError,
            ValueError,
        ) as error:
            raise _http_error(request, 422, "ATOMIC_SHADOW_PLAN_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/campaigns/{campaign_id}/shadow-plans",
        dependencies=[Depends(_mutation_guard)],
        status_code=201,
    )
    def create_shadow_evidence_plan_endpoint(
        campaign_id: str,
        body: ShadowEvidencePlanRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        from apex.application.shadow_operations import (
            CreateShadowEvidencePlan,
            CreateShadowEvidencePlanCommand,
            ShadowEvidencePlanConfiguration,
            fixed_strategy_configuration_bindings,
        )

        try:
            plan, replay = CreateShadowEvidencePlan(shadow_operations_store).run(
                CreateShadowEvidencePlanCommand(
                    campaign_id=campaign_id,
                    name=body.name,
                    configuration=ShadowEvidencePlanConfiguration(
                        iterations=body.iterations,
                        interval_seconds=body.interval_seconds,
                        market_count=body.market_count,
                        max_pages=body.maximum_pages,
                        max_enrichments=body.maximum_enrichments,
                        max_markets_per_event=body.maximum_markets_per_event,
                        max_markets_per_series=body.maximum_markets_per_series,
                        orderbook_depth=body.orderbook_depth,
                        maximum_predictions_per_iteration=body.maximum_predictions_per_iteration,
                        maximum_opportunities_per_iteration=(
                            body.maximum_opportunities_per_iteration
                        ),
                        strategy_configuration_bindings=fixed_strategy_configuration_bindings(
                            market_count=body.market_count
                        ),
                    ),
                    idempotency_key=idempotency_key,
                    requested_at=datetime.now(tz=UTC),
                    requested_market_tickers=tuple(body.market_tickers or ()),
                )
            )
            return _envelope(
                {"plan": plan, "idempotent_replay": replay, "financial_authority": "NONE"}
            )
        except (AutopilotError, CampaignControlError, ValueValidationError) as error:
            raise _http_error(request, 422, "SHADOW_PLAN_REJECTED", str(error)) from error

    @app.get("/api/v1/shadow-plans/{plan_id}")
    def shadow_plan(plan_id: str, request: Request) -> dict[str, object]:
        value = shadow_operations_store.plan(plan_id)
        if value is None:
            raise _http_error(request, 404, "SHADOW_PLAN_NOT_FOUND", "shadow plan was not found")
        return _envelope(value)

    @app.get("/api/v1/shadow-plans/{plan_id}/cohort")
    def shadow_plan_cohort(plan_id: str, request: Request) -> dict[str, object]:
        if shadow_operations_store.plan(plan_id) is None:
            raise _http_error(request, 404, "SHADOW_PLAN_NOT_FOUND", "shadow plan was not found")
        return _envelope({"items": shadow_operations_store.cohort(plan_id)})

    @app.get("/api/v1/shadow-plans/{plan_id}/iterations")
    def shadow_plan_iterations(
        plan_id: str, request: Request, limit: int = 100
    ) -> dict[str, object]:
        _page(request, limit, 0)
        if shadow_operations_store.plan(plan_id) is None:
            raise _http_error(request, 404, "SHADOW_PLAN_NOT_FOUND", "shadow plan was not found")
        return _envelope({"items": shadow_operations_store.iterations(plan_id, limit=limit)})

    @app.get("/api/v1/shadow-plans/{plan_id}/predictions")
    def shadow_plan_predictions(
        plan_id: str, request: Request, limit: int = 100
    ) -> dict[str, object]:
        _page(request, limit, 0)
        if shadow_operations_store.plan(plan_id) is None:
            raise _http_error(request, 404, "SHADOW_PLAN_NOT_FOUND", "shadow plan was not found")
        return _envelope({"items": shadow_operations_store.predictions(plan_id, limit=limit)})

    def _shadow_plan_control(
        plan_id: str,
        action: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: str,
    ) -> dict[str, object]:
        try:
            value, replay = shadow_operations_store.control_plan(
                plan_id=plan_id,
                action=action,
                idempotency_key=idempotency_key,
                reason=body.operator_reason,
                requested_at=datetime.now(tz=UTC),
            )
            return _envelope(
                {"plan": value, "idempotent_replay": replay, "financial_authority": "NONE"}
            )
        except (AutopilotError, CampaignControlError, ValueValidationError) as error:
            raise _http_error(request, 423, "SHADOW_PLAN_CONTROL_REJECTED", str(error)) from error

    @app.post("/api/v1/shadow-plans/{plan_id}/pause", dependencies=[Depends(_mutation_guard)])
    def pause_shadow_plan(
        plan_id: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _shadow_plan_control(plan_id, "pause", body, request, idempotency_key)

    @app.post("/api/v1/shadow-plans/{plan_id}/resume", dependencies=[Depends(_mutation_guard)])
    def resume_shadow_plan(
        plan_id: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _shadow_plan_control(plan_id, "resume", body, request, idempotency_key)

    @app.post("/api/v1/shadow-plans/{plan_id}/cancel", dependencies=[Depends(_mutation_guard)])
    def cancel_shadow_plan(
        plan_id: str,
        body: ControlRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        return _shadow_plan_control(plan_id, "cancel", body, request, idempotency_key)

    @app.post("/api/v1/shadow-plans/{plan_id}/run-due", dependencies=[Depends(_mutation_guard)])
    def run_due_shadow_plan(
        plan_id: str,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        """Manually run one due iteration in-process; it never launches a shell worker."""

        from apex.cli import run_shadow_plan_iteration

        try:
            return _envelope(
                run_shadow_plan_iteration(
                    plan_id=plan_id,
                    owner_id=deterministic_id(
                        "operator_shadow_plan_run",
                        {"plan_id": plan_id, "idempotency_key": idempotency_key},
                    ),
                )
            )
        except (AutopilotError, CampaignControlError, ValueError) as error:
            raise _http_error(request, 422, "SHADOW_PLAN_RUN_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/campaigns/{campaign_id}/collect-shadow-outcomes",
        dependencies=[Depends(_mutation_guard)],
    )
    def collect_shadow_outcomes_endpoint(
        campaign_id: str,
        body: ShadowOutcomeCollectionRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        from apex.cli import collect_shadow_outcomes

        _ = idempotency_key
        try:
            return _envelope(
                collect_shadow_outcomes(
                    campaign_id=campaign_id, maximum_markets=body.maximum_markets
                )
            )
        except (AutopilotError, ValueError) as error:
            raise _http_error(
                request, 422, "PUBLIC_OUTCOME_COLLECTION_REJECTED", str(error)
            ) from error

    @app.get("/api/v1/campaigns/{campaign_id}/strategy-performance")
    def campaign_strategy_performance(
        campaign_id: str,
        request: Request,
        strategy_id: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, object]:
        if store.campaign_metadata(campaign_id) is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope(
            shadow_operations_store.performance_report(
                campaign_id=campaign_id,
                strategy_id=strategy_id,
                plan_id=plan_id,
                requested_at=datetime.now(tz=UTC),
            )
        )

    @app.get("/api/v1/strategies/{strategy_id}/performance")
    def strategy_performance_endpoint(
        strategy_id: str, campaign_id: str, plan_id: str | None = None
    ) -> dict[str, object]:
        return _envelope(
            shadow_operations_store.performance_report(
                campaign_id=campaign_id,
                strategy_id=strategy_id,
                plan_id=plan_id,
                requested_at=datetime.now(tz=UTC),
            )
        )

    @app.post(
        "/api/v1/campaigns/{campaign_id}/theses",
        dependencies=[Depends(_mutation_guard)],
    )
    def submit_thesis(
        campaign_id: str,
        body: OperatorThesisRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        from apex.cli import submit_operator_thesis

        try:
            return _envelope(
                submit_operator_thesis(
                    campaign_id=campaign_id,
                    market_snapshot_id=body.market_snapshot_id,
                    point_probabilities=tuple(sorted(body.point_probabilities.items())),
                    conservative_probabilities=tuple(
                        sorted(body.conservative_probabilities.items())
                    ),
                    resolution_at=body.resolution_at,
                    requested_risk_fraction=body.requested_risk_fraction,
                    thesis_summary=body.thesis_summary,
                    evidence_references=tuple(body.evidence_references),
                    correlation_buckets=tuple(sorted(body.correlation_buckets)),
                    operator_note=body.operator_note,
                    idempotency_key=idempotency_key,
                )
            )
        except CampaignControlError as error:
            raise _http_error(
                request,
                423,
                "CAMPAIGN_NOT_RUNNABLE",
                str(error),
                entity_type="campaign",
                entity_id=campaign_id,
            ) from error
        except (AutopilotError, ValueValidationError) as error:
            raise _http_error(
                request,
                422,
                "THESIS_REJECTED",
                str(error),
                entity_type="campaign",
                entity_id=campaign_id,
            ) from error

    @app.get("/api/v1/retained-markets/{snapshot_id}")
    def retained_market(snapshot_id: str, request: Request) -> dict[str, object]:
        """Expose only already-retained, public market terms for thesis review."""

        from apex.adapters.postgres import postgres_strategy_shadow_store

        try:
            market = postgres_strategy_shadow_store(engine).retained_market(snapshot_id)
            return _envelope(
                {
                    "market": market,
                    "market_probability": market.market_probability,
                }
            )
        except AutopilotError as error:
            raise _http_error(
                request,
                404,
                "RETAINED_MARKET_NOT_FOUND",
                str(error),
                entity_type="market_snapshot",
                entity_id=snapshot_id,
            ) from error

    @app.get("/api/v1/campaigns/{campaign_id}/predictions")
    def campaign_predictions(
        campaign_id: str,
        request: Request,
        strategy_id: str | None = None,
        resolved: bool | None = None,
    ) -> dict[str, object]:
        from apex.cli import list_shadow_predictions

        if store.campaign_metadata(campaign_id) is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope(
            {
                "items": list_shadow_predictions(
                    campaign_id=campaign_id, strategy_id=strategy_id, resolved=resolved
                )
            }
        )

    @app.get("/api/v1/predictions/{prediction_id}")
    def prediction_detail(prediction_id: str, request: Request) -> dict[str, object]:
        from apex.cli import shadow_prediction_detail

        try:
            return _envelope(shadow_prediction_detail(prediction_id))
        except AutopilotError as error:
            raise _http_error(
                request,
                404,
                "PREDICTION_NOT_FOUND",
                str(error),
                entity_type="prediction",
                entity_id=prediction_id,
            ) from error

    @app.post(
        "/api/v1/predictions/{prediction_id}/evaluate",
        dependencies=[Depends(_mutation_guard)],
    )
    def evaluate_prediction_endpoint(
        prediction_id: str,
        body: PredictionEvaluateRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        from apex.cli import evaluate_shadow_prediction

        try:
            return _envelope(
                evaluate_shadow_prediction(
                    prediction_id=prediction_id,
                    settlement_fact_id_value=body.settlement_fact_id,
                    operator_note=body.operator_note,
                    idempotency_key=idempotency_key,
                )
            )
        except (AutopilotError, ValueValidationError) as error:
            raise _http_error(
                request,
                422,
                "PREDICTION_EVALUATION_REJECTED",
                str(error),
                entity_type="prediction",
                entity_id=prediction_id,
            ) from error

    @app.get("/api/v1/campaigns/{campaign_id}/strategies/scorecards")
    def campaign_scorecards(campaign_id: str, request: Request) -> dict[str, object]:
        if store.campaign_metadata(campaign_id) is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope({"items": store.strategy_scorecards(campaign_id)})

    @app.get("/api/v1/campaigns/{campaign_id}/portfolio")
    def portfolio(campaign_id: str, request: Request) -> dict[str, object]:
        value = store.campaign_detail(campaign_id)
        if value is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope(value["portfolio"])

    @app.get("/api/v1/campaigns/{campaign_id}/reservations")
    def reservations(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope({"items": store.reservations(campaign_id, limit=limit, offset=offset)})

    @app.get("/api/v1/campaigns/{campaign_id}/orders")
    def orders(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope({"items": store.orders(campaign_id, limit=limit, offset=offset)})

    @app.get("/api/v1/campaigns/{campaign_id}/positions")
    def positions(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope({"items": store.positions(campaign_id, limit=limit, offset=offset)})

    @app.get("/api/v1/campaigns/{campaign_id}/mark-evidence")
    def mark_evidence(campaign_id: str, request: Request) -> dict[str, object]:
        if store.campaign_metadata(campaign_id) is None:
            raise _http_error(request, 404, "CAMPAIGN_NOT_FOUND", "campaign was not found")
        return _envelope({"items": store.available_mark_evidence(campaign_id)})

    @app.get("/api/v1/positions/{position_id}")
    def position(position_id: str, request: Request) -> dict[str, object]:
        value = store.position_detail(position_id)
        if value is None:
            raise _http_error(
                request,
                404,
                "POSITION_NOT_FOUND",
                "position was not found",
                entity_type="position",
                entity_id=position_id,
            )
        return _envelope(value)

    @app.get("/api/v1/campaigns/{campaign_id}/ledger")
    def ledger(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope({"items": store.ledger(campaign_id, limit=limit, offset=offset)})

    @app.get("/api/v1/campaigns/{campaign_id}/activity")
    def activity(
        campaign_id: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope(
            {"items": store.activity(campaign_id=campaign_id, limit=limit, offset=offset)}
        )

    @app.get("/api/v1/activity")
    def all_activity(request: Request, limit: int = 100, offset: int = 0) -> dict[str, object]:
        _page(request, limit, offset)
        return _envelope({"items": store.activity(campaign_id=None, limit=limit, offset=offset)})

    @app.post(
        "/api/v1/campaigns/{campaign_id}/marks",
        dependencies=[Depends(_mutation_guard)],
    )
    def marks(
        campaign_id: str,
        body: MarkRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        retained_evidence_ids = {
            str(item["evidence_id"]) for item in store.available_mark_evidence(campaign_id)
        }
        if body.orderbook_run_id not in retained_evidence_ids:
            raise _http_error(
                request,
                422,
                "RETAINED_EVIDENCE_REQUIRED",
                "a Position can be marked only from an existing retained evidence ID",
            )
        try:
            from apex.cli import paper_mark_positions

            result = paper_mark_positions(campaign_id, body.orderbook_run_id)
            audit = store.record_action(
                campaign_id=campaign_id,
                action_type=OperatorActionType.MARK_POSITIONS,
                idempotency_key=idempotency_key,
                operator_note=body.operator_note,
                target_entity_ids=(campaign_id, body.orderbook_run_id),
                reason_codes=("RETAINED_EVIDENCE_MARK",),
                request=body.model_dump(),
                result=result,
            )
            return _envelope({"marks": result, "audit": audit})
        except (ValueError, MissingPersistenceStateError) as error:
            raise _http_error(request, 422, "MARK_REJECTED", str(error)) from error

    @app.post(
        "/api/v1/positions/{position_id}/settle",
        dependencies=[Depends(_mutation_guard)],
    )
    def settle(
        position_id: str,
        body: SettleRequest,
        request: Request,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, object]:
        value = store.position_detail(position_id)
        if value is None:
            raise _http_error(request, 404, "POSITION_NOT_FOUND", "position was not found")
        campaign_id = str(value["campaign_id"])
        retained_fact_ids = {
            str(item["settlement_fact_id"])
            for item in store.available_settlement_facts(position_id)
        }
        if body.settlement_fact_id not in retained_fact_ids:
            raise _http_error(
                request,
                422,
                "RETAINED_FACT_REQUIRED",
                "a Position can settle only from a compatible existing SettlementFact ID",
            )
        try:
            from apex.cli import paper_settle_position

            result = paper_settle_position(position_id, body.settlement_fact_id)
            audit = store.record_action(
                campaign_id=campaign_id,
                action_type=OperatorActionType.SETTLE_POSITION,
                idempotency_key=idempotency_key,
                operator_note=body.operator_note,
                target_entity_ids=(position_id, body.settlement_fact_id),
                reason_codes=("RETAINED_FACT_SETTLEMENT",),
                request=body.model_dump(),
                result={"settlement_id": result},
            )
            return _envelope({"settlement_id": result, "audit": audit})
        except (ValueError, MissingPersistenceStateError) as error:
            raise _http_error(request, 422, "SETTLEMENT_REJECTED", str(error)) from error

    static = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="operator-ui")
    return app


app = create_app()
