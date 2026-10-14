"""Bounded adapters that delegate operating-cycle tasks to existing services."""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy.engine import Engine

from apex.adapters.alpaca_options_quote_source import AlpacaOptionsChainCaptureClient
from apex.adapters.coinbase_universe_public import (
    MAX_CANDLES_PER_REQUEST,
    CoinbasePublicUniverseSource,
)
from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_followup_sources import KalshiFollowUpPublicMarketSource
from apex.adapters.counterfactual_ledger_postgres import BatchedLedgerEvidenceSource
from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.counterfactual_outcome_sources import RetainedCounterfactualOutcomeSource
from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
    PostgresCrossVenueAllocationSource,
)
from apex.adapters.crypto_market_pulse_postgres import PostgresCryptoMarketPulseRepository
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.crypto_stateful_research_postgres import (
    PostgresCryptoStatefulResearchRepository,
)
from apex.adapters.first_generated_research_method_sources import (
    PostgresCurrentOpportunityOperationalEvidenceSource,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.kalshi_baseline_quote_postgres import PostgresKalshiBaselineQuoteEvidenceSource
from apex.adapters.kalshi_operating_postgres import (
    PostgresKalshiCurrentOpportunitySource,
    PostgresKalshiOperatingRepository,
)
from apex.adapters.kalshi_public import (
    KalshiPublicEventNormalizer,
    KalshiPublicMarketDataClient,
    KalshiPublicMarketNormalizer,
    KalshiPublicOrderbookDataClient,
)
from apex.adapters.kalshi_resolution_postgres import PostgresKalshiResolutionFactRepository
from apex.adapters.live_market_observation_postgres import (
    PostgresLiveMarketObservationRepository,
)
from apex.adapters.operating_cycle_postgres import PostgresOperatingStateSource
from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_research_capture import (
    AlpacaOptionsResearchEvidenceCapture,
    OptionsResearchEvidenceCaptureCommand,
    resolve_options_capture_due_decision,
)
from apex.adapters.options_research_operations_postgres import (
    PostgresOptionsResearchOperationsRepository,
)
from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
from apex.adapters.performance_attribution_postgres import (
    PostgresPerformanceAttributionRepository,
)
from apex.adapters.performance_attribution_sources import (
    RetainedPerformanceAttributionSource,
)
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.portfolio_state_sources import PostgresPortfolioStateSource
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
    PostgresRetainedResearchKnowledgeSource,
)
from apex.adapters.research_operations_sources import RetainedResearchAgendaSource
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.adapters.strategy_evaluation_history_postgres import (
    PostgresInternalStrategyEvaluationRepository,
)
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialClosureRepository,
    PostgresStrategyPaperTrialRepository,
)
from apex.adapters.strategy_trial_outcome_assessment_postgres import (
    PostgresStrategyTrialOutcomeAssessmentRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    PostgresStrategyVersionProvenanceRepository,
)
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.adapters.total_market_context_sources import RetainedTotalMarketContextSource
from apex.application.capture_public_orderbook_session import (
    CaptureBoundedPublicOrderbookSession,
)
from apex.application.counterfactual_classification import SyncCounterfactualClassification
from apex.application.counterfactual_followup import (
    AssessFollowUpEvidenceQuality,
    FollowUpSourceUnavailable,
    KalshiMarketRead,
    KalshiMarketSource,
    SyncFollowUpObservations,
)
from apex.application.counterfactual_ledger import SyncCounterfactualLedger
from apex.application.counterfactual_outcome import RefreshCounterfactualOutcomeAggregates
from apex.application.cross_venue_allocation import MaterializeCrossVenueAllocation
from apex.application.crypto_market_pulse import (
    ExecuteCryptoMarketPulseRefresh,
    RunCurrentCryptoRelativeStrength,
    RunCurrentCryptoReversalRegime,
    current_crypto_method_inputs,
)
from apex.application.crypto_paper import (
    ApplyCryptoPaperMarketEvidence,
    AuthorizeCryptoPaperOpportunity,
    ConstructCryptoPaperOrderIntent,
    CreateCryptoPaperAccount,
    ExpireCryptoPaperOrder,
    PreviewCryptoPaperOrder,
    ReconcileCryptoPaperAccount,
    RegisterCryptoPaperProviderContract,
    RetainCryptoPaperCapability,
    SubmitCryptoPaperOrder,
)
from apex.application.crypto_stateful_research import RunCurrentCryptoStatefulMethod
from apex.application.crypto_universe import (
    CaptureCryptoCalibrationHistory,
    CaptureCryptoUniverseObservations,
    CapturePublicCryptoProducts,
    MaterializeCryptoUniverse,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)
from apex.application.first_generated_research_method import (
    MaterializeFirstGeneratedResearchMethodRun,
)
from apex.application.investment_mandate import EvaluateOpportunityAgainstMandate
from apex.application.kalshi_baseline_quote import (
    DEFAULT_KALSHI_BASELINE_QUOTE_SWEEP_LIMIT,
    SyncKalshiBaselineQuoteEvidence,
)
from apex.application.kalshi_operating import (
    ApplyKalshiPaperFill,
    AuthorizeKalshiPaperOpportunity,
    CreateKalshiPaperAccount,
    ExecuteKalshiCurrentRefresh,
    ExpireKalshiPaperOrder,
    KalshiCurrentRefreshCommand,
    PreviewKalshiPaperOrder,
    ReconcileKalshiPaperAccount,
    SubmitKalshiPaperOrder,
)
from apex.application.live_market_observation import ObtainContemporaneousMarketObservation
from apex.application.options_quote_intelligence import ResolveRealizedVolatility
from apex.application.options_structures import (
    CreateOptionsFeePolicy,
    CreateOptionsSlippagePolicy,
    CreateOptionsStructureResearchPolicy,
    CreateStructurePricingPolicy,
)
from apex.application.performance_attribution import (
    MaterializePerformanceAttribution,
    MaterializeStrategyHealth,
)
from apex.application.portfolio_state import BuildPortfolioSnapshot, PortfolioSourceSelection
from apex.application.research_operations import (
    GenerateResearchProposals,
    RefreshResearchAgenda,
    proposal_state,
)
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    CapturedKalshiPublicResponse,
    ScanPublicBinaryComplements,
)
from apex.application.scan_public_threshold_ladders import (
    PublicThresholdLadderScanRequest,
    ScanPublicThresholdLadders,
)
from apex.application.securities_structure_outcome import (
    RefreshSecuritiesStructureOutcomeAggregates,
)
from apex.application.shared_opportunity import MaterializeSharedOpportunitySet
from apex.application.strategy_evaluation_history import RecordInternalStrategyEvaluation
from apex.application.strategy_paper_trial import ExecuteStrategyPaperTrial
from apex.application.strategy_paper_trial_lifecycle import (
    RecordStrategyPaperTrialClosed,
    RecordStrategyPaperTrialOpened,
)
from apex.application.strategy_trial_outcome_assessment import AssessClosedStrategyPaperTrials
from apex.application.strategy_version_provenance import (
    RecordEvaluationSubject,
    RecordProvenancedTrialOpened,
    RecordStrategyVersionTransition,
    StrategyVersionProvenanceResearchError,
)
from apex.application.total_market_context import (
    AttachTotalMarketContext,
    MaterializeTotalMarketContextSnapshot,
)
from apex.domain.counterfactual_classification import CURRENT_CLASSIFIER_VERSION
from apex.domain.counterfactual_followup import (
    CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
    CURRENT_FOLLOWUP_POLICY_VERSIONS,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    DecisionFollowUpObservation,
    SecuritiesTerminalEvidenceTiming,
    current_followup_policy_venues,
)
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_POLICY_V2,
    COUNTERFACTUAL_OUTCOME_POLICY_V3,
)
from apex.domain.crypto_market_pulse import CryptoPulseCheckpoint, RefreshState, completed_utc_day
from apex.domain.crypto_paper import (
    CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1,
    MAX_ORDER_NOTIONAL,
    CryptoPaperAccountState,
    CryptoPaperAuthorization,
    CryptoPaperCancellation,
    CryptoPaperEligibility,
    CryptoPaperError,
    CryptoPaperFillApplication,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperProviderContract,
    CryptoPaperState,
    PaperEligibilityState,
    PaperOrderState,
    PaperOrderType,
    PaperPricingSource,
    PaperSide,
    RetainedPaperPriceEvidence,
    build_provider_contract,
    evaluate_paper_eligibility,
    inspect_paper_capability,
    open_trial_orders_by_strategy,
    primary_fill_journal_id,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.crypto_universe import CryptoUniverseSnapshot
from apex.domain.investment_mandate import DEFAULT_MANDATE_LINEAGE_ID
from apex.domain.kalshi_operating import (
    MAX_PAPER_ORDER_RISK,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiPaperAuthorization,
    KalshiPaperEligibility,
    KalshiPaperOrderState,
)
from apex.domain.live_market_observation import (
    ContemporaneousObservationDisposition,
    ContemporaneousObservationResult,
    LiveObservationType,
)
from apex.domain.operating_cycle import (
    DueTask,
    OperatingCycleError,
    OperatingCyclePlan,
    OperatingTaskStatus,
    OperatingTaskType,
    RetainedOperatingState,
    TaskExecution,
)
from apex.domain.options_research_operations import (
    ChainCoverageQualityPolicy,
    OptionsCaptureDueStatus,
)
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
    StructurePricingPolicy,
)
from apex.domain.performance_attribution import (
    AttributionSourceSnapshot,
    PaperEvidenceClass,
    StrategyHealthAssessment,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1
from apex.domain.research_evidence import EvidenceClass
from apex.domain.research_operations import ResearchProposalState
from apex.domain.securities_structure_outcome import (
    CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION,
)
from apex.domain.shared_opportunity import (
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
    evaluate_eligibility,
    prioritize,
)
from apex.domain.strategy_benchmark import (
    BENCHMARK_INSTRUMENT,
    StrategyBenchmarkError,
    canonical_strategy_benchmarks,
    simulate_strategy_backtest,
)
from apex.domain.strategy_definition import StrategyDefinition
from apex.domain.strategy_evaluation import (
    StrategyEvaluationError,
    StrategyEvaluationResult,
    evaluate_external_strategy,
    promote_to_paper_eligible,
)
from apex.domain.strategy_paper_trial import (
    StrategyPaperOrderPlacement,
    StrategyPaperTrialError,
    StrategyPaperTrialOutcome,
    build_attribution_source_from_trial,
    build_strategy_opportunity_projection,
    daily_candles_from_close_observations,
    price_evidence_from_live_observation,
    recurring_strategy_paper_trial_seed,
)
from apex.domain.strategy_paper_trial_evidence_coverage import (
    TrialEvidenceCoverage,
    evaluate_trial_evidence_coverage,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    CloseAttemptState,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
    TrialHorizonState,
    TrialRetrialEligibility,
    adopt_legacy_trial,
    build_strategy_paper_trial_closure_record,
    build_strategy_paper_trial_record,
    build_unfilled_expiry_closure_record,
    classify_close_attempts,
    evaluate_retrial_eligibility,
    evaluate_trial_horizon,
    next_trial_generation,
    promoted_trial_slot_version,
    strategy_paper_trial_id,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionProvenanceError,
    StrategyVersionRegistry,
    StrategyVersionTransitionKind,
)
from apex.domain.values import content_digest, deterministic_id

GENUINE_CRYPTO_PAPER_ACCOUNT_ID = "genuine-crypto-paper-account"
GENUINE_KALSHI_PAPER_ACCOUNT_ID = "genuine-kalshi-paper-account"
# Recurring Strategy PAPER Orchestration v1: a dedicated, separate PAPER
# account -- never GENUINE_CRYPTO_PAPER_ACCOUNT_ID -- so this new,
# structurally-independent strategy-driven path can never collide with, or
# be silently starved by, the older Cross-Venue-Allocation path's own
# MAX_OPEN_ORDERS_PER_PRODUCT=1 usage of the same product on the same
# account (see _execute_strategy_paper_trial's own docstring for the full
# independence rationale).
GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID = "genuine-strategy-crypto-paper-account"
GENUINE_PAPER_OPENING_CASH = "1000"
GENUINE_PAPER_ACTOR = "system:operating-cycle"
STRATEGY_PAPER_TRIAL_ACTOR = "system:operating-cycle:strategy-paper-trial"
# Bounded, deterministic allocator (Phase E): every canonical benchmark
# candidate (domain.strategy_benchmark.canonical_strategy_benchmarks)
# targets the identical instrument (BENCHMARK_INSTRUMENT), and domain.
# crypto_paper's own MAX_OPEN_ORDERS_PER_PRODUCT=1 already forbids more than
# one open order per product per account -- allowing more than one new
# strategy-driven trial per cycle on the same instrument would only ever be
# duplicate/overlapping exposure to the identical underlying asset, never
# genuine diversification. Conservative, and given today's single-
# instrument candidate universe, structurally sufficient rather than an
# arbitrarily chosen larger number.
MAX_NEW_STRATEGY_PAPER_TRIALS_PER_CYCLE = 1
# Conservative, previously-established Apex assumption (matches the fixed 60-bps
# fee / 10-bps-slippage-per-side used by Genuine Crypto Historical Validation v1)
# used only when no genuine authenticated fee-tier evidence is retained.
CONSERVATIVE_CRYPTO_FEE_BPS = Decimal(60)
CONSERVATIVE_CRYPTO_SPREAD_BPS = Decimal(10)
CONSERVATIVE_CRYPTO_SLIPPAGE_BPS = Decimal(10)
CRYPTO_EVIDENCE_MAXIMUM_AGE = timedelta(minutes=30)
CRYPTO_ORDER_VALID_FOR = timedelta(hours=4)
KALSHI_ORDER_VALID_FOR = timedelta(hours=4)
# Counterfactual Follow-Up Recurring Integration v1 (Part "REQUEST BUDGETING"):
# a deterministic, fixed, per-cycle bound -- not configurable at runtime, the
# same "named constant, not a CLI flag" idiom every other recurring-cycle
# bound in this module already uses. ``sample_limit`` mirrors the manual
# ``followup-sync`` CLI's own ``DEFAULT_SAMPLE_LIMIT`` (kept as an
# independent constant, not an import of the CLI default, so a future change
# to one cannot silently retune the other) and bounds total genuine
# attempts (CRYPTO local reads + KALSHI provider reads) considered this
# cycle, oldest-due-first. The Kalshi bound is strictly a *provider request*
# budget, separate from and tighter than the attempt bound, since a single
# Kalshi attempt can cost up to two genuine GETs (event lookup, then a
# single-market fallback) while a CRYPTO attempt always costs zero.
#
# Counterfactual Forward Outcome Temporal Integrity v1: this is now a bound
# *per policy version*, because the current per-venue selection runs one
# independently-cursored bounded scan per distinct version (CRYPTO under
# FOLLOWUP_POLICY_V3, KALSHI and SECURITIES under FOLLOWUP_POLICY_V2).  The
# value is unchanged, deliberately: it was tuned against real historical
# throughput evidence (see COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY
# below), and *splitting* it across the runs instead would have silently
# halved the attempt throughput of every venue -- a throughput regression
# unrelated to anything this milestone set out to change, and one that would
# have landed hardest on KALSHI, whose follow-up behavior did not change at
# all.  Each venue group therefore keeps exactly the bound it already had,
# and the honest per-cycle total is derived, named, and reported rather than
# left implicit.
#
# Genuine *provider* load does not scale with the number of runs: the Kalshi
# request budget below is enforced by a single counter shared across every
# run of one cycle (the sync service is constructed once and executed once
# per version), and a CRYPTO attempt costs zero provider requests by
# construction.  What does scale is local, already-retained-evidence reads,
# which is what this bound has always actually governed.
COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION = 25
COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE = (
    COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION * len(CURRENT_FOLLOWUP_POLICY_VERSIONS)
)
COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE = 20
COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED = (
    "COUNTERFACTUAL_FOLLOWUP_KALSHI_REQUEST_BUDGET_EXHAUSTED_THIS_CYCLE"
)
# Counterfactual Follow-Up Starvation Fix v1: a second, independent,
# deliberately much larger bound -- kept as its own adapter-local constant
# for the same reason ``COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE`` is
# (not an import of ``application.counterfactual_followup.
# DEFAULT_CANDIDATE_SCAN_LIMIT``, so a future change to one cannot silently
# retune the other).  See ``SyncFollowUpObservations.execute``'s own
# docstring: this is a secondary wall-clock safety net, never the mechanism
# that keeps a permanently-unavailable pair from starving later work.
# Counterfactual Forward Outcome Temporal Integrity v1: per policy version,
# for the same reason -- and the same unchanged value -- as the attempt
# bound above.
COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION = 2000
COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_CYCLE = (
    COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION
    * len(CURRENT_FOLLOWUP_POLICY_VERSIONS)
)


# Counterfactual Follow-Up Backlog Throughput v1: real historical evidence
# (9/9 recurring cycles since this task's own integration) shows every
# genuine daily invocation fills its entire ``COUNTERFACTUAL_FOLLOWUP_
# SAMPLE_LIMIT_PER_CYCLE`` attempt budget while using well under 6% of
# ``COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_CYCLE`` (peak 117/2000)
# and well under the independent Kalshi request budget (peak 20/20, on
# exactly one historical day; every other day 2-11) -- i.e. the *only*
# thing that has ever bound this task's daily throughput is the fact that
# it dispatches exactly once per UTC calendar day, not any genuine
# provider/DB/latency constraint. ``TaskExecution.awaits_intraday_
# continuation`` (see its own docstring) lets this exact, unmodified,
# already-proven-safe ``sample_limit``/``candidate_scan_limit``/Kalshi-
# budget-shaped round repeat across multiple same-day ticks instead of
# raising any one of those per-round constants. This is the one additional,
# independent, named daily ceiling on top of that -- total genuinely new
# observations created this UTC calendar day, across every round -- kept
# deliberately far below what would be needed to exhaust either
# ``candidate_scan_limit`` or the Kalshi budget purely by round count -- an
# explicit, reviewable belt-and-suspenders ceiling checked before every
# additional same-day round (see the ``COUNTERFACTUAL_FOLLOWUP_REFRESH``
# executor branch below), never relied on alone: the *primary* stopping
# signal is simply a round genuinely not filling its own ``sample_limit``
# (mathematically guaranteed to happen within a bounded, shrinking number
# of rounds, since ``SyncFollowUpObservations``'s own durable cursor never
# revisits an already-observed pair -- see its module docstring).
# Counterfactual Follow-Up Daily Ceiling Lane Scope v1: per policy version,
# for exactly the reason -- and with exactly the same unchanged value -- as
# the two per-round bounds above.
#
# This constant was authored when there was precisely one current policy
# version, so "the whole subsystem's daily budget" and "this lane's daily
# budget" were the same number and nothing had to choose between them.
# Counterfactual Forward Outcome Temporal Integrity v1 made the two
# per-round bounds per-policy-version and said plainly why: splitting one
# bound across the runs "would have silently halved the attempt throughput
# of every venue -- a throughput regression unrelated to anything this
# milestone set out to change, and one that would have landed hardest on
# KALSHI, whose follow-up behavior did not change at all". This bound was
# left global, which produced that exact regression one level up, via round
# count rather than via attempts. Measured against real retained evidence:
#
#   UTC day     CRYPTO V3  KALSHI V2  SECURITIES V2  total  last observation
#   2026-09-21          0        128            822   1012         23:10:16Z
#   2026-09-22          0        139            797   1020         03:21:32Z
#   2026-09-23          0        106            848   1024         03:20:29Z
#   2026-09-24        500         91            409   1000         01:35:16Z
#   2026-09-25        500          6            494   1000         01:35:16Z
#
# The arithmetic is exact. Each same-day round creates up to
# ``COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION`` observations
# *per lane*, and continuation stops at this ceiling, so the ceiling buys
# ``ceiling / (sample_limit * lanes)`` rounds: 1000/25 = 40 rounds with one
# lane, 1000/50 = 20 with two. Adding the CRYPTO V3 lane therefore halved
# the *rounds*, and with them every unchanged lane's daily throughput --
# SECURITIES 822 -> 409, KALSHI 128 -> 91 -- while the subsystem finished
# its whole day by 01:35Z and then sat idle for the remaining ~22 hours.
#
# Scoping this bound to the same semantic lane the per-round bounds already
# use restores the pre-V3 contract exactly, and provably does not exceed it:
#
# * the V2 lane (KALSHI + SECURITIES) gets 1000 observations/day, which is
#   the identical number it had when it was the only lane -- 40 rounds at 25
#   created per round, exactly as before;
# * genuine *provider* load is bounded by round count, since
#   ``COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE`` is enforced by
#   a single counter shared across every lane run of one round. Pre-V3
#   worst case was 40 rounds x 20 = 800 Kalshi requests/day; post-V3 it fell
#   to 20 x 20 = 400; this restores it to 800 and no further. Kalshi appears
#   in exactly one lane, so a second lane cannot buy it more rounds;
# * the CRYPTO V3 lane costs zero provider requests by construction, so its
#   own 1000 observations/day are purely local reads and writes.
#
# The total is derived, named, and reported rather than left implicit --
# the same idiom as ``COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE`` and
# ``COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_CYCLE`` above -- so
# total daily work still cannot grow without a reviewed edit here, and an
# operator reading a diagnostic sees both numbers rather than inferring one
# from the other.
COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION = 1000
COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY = (
    COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
    * len(CURRENT_FOLLOWUP_POLICY_VERSIONS)
)

# Autonomy Audit v1 (CRYPTO Follow-Up Evidence Eligibility & Contaminated
# Observation Remediation v1's own required follow-up). ``application.
# counterfactual_followup.AssessFollowUpEvidenceQuality`` shipped CLI-
# triggered-only (mirroring ``counterfactual_classification``'s own
# precedent) -- correct for the one-time census of the 92 rows retained
# before it existed, but it left every observation the *recurring* cycle
# creates from now on permanently ``UNASSESSED`` (fail-closed excluded from
# downstream attribution, per ``domain.counterfactual_followup.is_evidence_
# eligible_for_downstream_attribution``) unless a human re-ran that CLI
# after every batch of new evidence -- exactly the recurring-human-
# maintenance dependency Apex must not have.
#
# Fixed by composing the identical, unmodified ``AssessFollowUpEvidenceQuality``
# into this same ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` branch, immediately
# after ``SyncFollowUpObservations`` runs -- no second ``OperatingTaskType``,
# no new scheduler. Bounded via ``PostgresCounterfactualFollowUpRepository.
# unassessed_observations`` (Part: see its own docstring) rather than the
# CLI's own full-population source, so this sweep's cost never grows with
# the size of the whole retained corpus, only with the size of its own
# backlog gap -- kept comfortably above ``COUNTERFACTUAL_FOLLOWUP_SAMPLE_
# LIMIT_PER_CYCLE`` (the most new observations one cycle can ever create)
# so the sweep always keeps pace with genuinely new evidence with headroom
# left over to drain any pre-existing backlog (today, at most the 92 legacy
# rows the one-time CLI census already covers) within a handful of cycles.
# A pure local read plus a pure per-row function, exactly like the CLI's own
# read path -- zero provider requests, zero provider writes, authority NONE,
# idempotent (``save_assessment``'s existing hash-conflict-or-replay
# contract, unchanged), and self-healing by construction: it queries
# retained state for "no assessment row yet" rather than this cycle's own
# in-memory sync results, so a crash between a prior cycle's observation
# commit and its assessment commit is repaired by whichever future cycle
# next reaches this branch, with no special-cased recovery path.
COUNTERFACTUAL_FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT_PER_CYCLE = 100


class _BoundedUnassessedFollowUpEvidenceSource:
    """Implements ``application.counterfactual_followup.
    FollowUpEvidenceQualitySource`` for the recurring cycle's own sweep
    (Part: see ``COUNTERFACTUAL_FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT_PER_
    CYCLE``'s own module-level docstring for the full rationale).

    Deliberately module-level rather than a local class defined inside
    ``execute`` the way ``_BoundedKalshiFollowUpSource`` below is: this
    class captures no per-cycle mutable state (unlike that Kalshi wrapper,
    which closes over a request counter that must reset every call) -- it
    is a plain, reusable, constructor-injected adapter, so it does not need
    to be redefined on every invocation.

    ``.observations()`` deliberately returns only the bounded, still-
    unassessed set (``PostgresCounterfactualFollowUpRepository.
    unassessed_observations``) -- never every retained observation the way
    the CLI's own full-population source does -- so a recurring cycle's
    sweep cost never grows with the total size of the retained corpus."""

    def __init__(
        self,
        repository: PostgresCounterfactualFollowUpRepository,
        limit: int,
        *,
        assessment_policy_version: str = CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
        venue: SharedVenue | None = None,
    ) -> None:
        self._repository = repository
        self._limit = limit
        # Securities Follow-Up Temporal Integrity v1: the SECURITIES-only
        # ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2`` lane reuses this exact
        # bounded backlog source, narrowed to its own version and venue.
        self._assessment_policy_version = assessment_policy_version
        self._venue = venue

    def observations(self) -> tuple[DecisionFollowUpObservation, ...]:
        return self._repository.unassessed_observations(
            assessment_policy_version=self._assessment_policy_version,
            limit=self._limit,
            venue=self._venue,
        )

    def resolve_observation_evidence_lineage(
        self, observation: DecisionFollowUpObservation
    ) -> tuple[EvidenceClass | None, EvidenceClass | None, bool]:
        return self._repository.resolve_observation_evidence_lineage(observation)

    def resolve_securities_terminal_evidence_timing(
        self, observation: DecisionFollowUpObservation
    ) -> SecuritiesTerminalEvidenceTiming:
        return self._repository.resolve_securities_terminal_evidence_timing(observation)


def _round_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


class ExistingCapabilityOperatingTaskExecutor:
    """Coordinate existing exact services without adding provider capability."""

    def __init__(
        self,
        engine: Engine,
        state_source: PostgresOperatingStateSource,
        *,
        options_capture_client: AlpacaOptionsChainCaptureClient | None = None,
        followup_kalshi_source: KalshiMarketSource | None = None,
    ) -> None:
        self._engine = engine
        self._state_source = state_source
        # Optional, defaults to the real, unmodified
        # ``KalshiFollowUpPublicMarketSource`` -- unattended production
        # operation never passes this and is therefore byte-for-byte
        # unchanged. Exists solely so a real-Postgres regression test can
        # drive the COUNTERFACTUAL_FOLLOWUP_REFRESH branch through this
        # exact executor with a synthetic Kalshi source instead of making a
        # genuine network request -- mirrors ``options_capture_client``
        # immediately above exactly, same rationale, same shape.
        self._followup_kalshi_source = followup_kalshi_source
        # Optional, defaults to the real Alpaca-backed client -- unattended
        # production operation never passes this and is therefore byte-for-
        # byte unchanged. Exists solely so a real-Postgres regression test
        # can drive ``AlpacaOptionsResearchEvidenceCapture`` through this
        # exact executor with a synthetic, credentials-available capture
        # client instead of needing genuine Alpaca credentials -- mirrors
        # the identical, already-reviewed optional-injection pattern
        # ``AlpacaOptionsResearchEvidenceCapture.__init__`` itself already
        # uses one layer down (Options Automatic Capture Parent Persistence
        # & Partial-Evidence Recovery v1, Part 20).
        self._options_capture_client = options_capture_client
        self._crypto = PostgresCryptoMarketPulseRepository(engine)
        self._crypto_stateful = PostgresCryptoStatefulResearchRepository(engine)
        self._crypto_paper = PostgresCryptoPaperRepository(engine)
        self._kalshi = PostgresKalshiOperatingRepository(engine)
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._context = PostgresTotalMarketContextRepository(engine)
        self._cross_venue = PostgresCrossVenueAllocationRepository(engine)
        self._cross_venue_source = PostgresCrossVenueAllocationSource(engine)
        self._performance = PostgresPerformanceAttributionRepository(engine)
        self._performance_source = RetainedPerformanceAttributionSource(engine)
        self._research_operations = PostgresResearchOperationsRepository(engine)
        self._research_agenda_source = RetainedResearchAgendaSource(engine)
        self._options_quote = PostgresOptionsQuoteIntelligenceRepository(engine)
        self._options_structures = PostgresOptionsStructuresRepository(engine)
        self._options_checkpoints = PostgresOptionsResearchOperationsRepository(engine)
        self._portfolio = PostgresPortfolioSnapshotRepository(engine)
        self._portfolio_source = PostgresPortfolioStateSource(engine)
        self._mandate = PostgresInvestmentMandateRepository(engine)
        self._followup = PostgresCounterfactualFollowUpRepository(engine)
        self._counterfactual_outcome = PostgresCounterfactualOutcomeRepository(engine)
        self._counterfactual_outcome_source = RetainedCounterfactualOutcomeSource(engine)
        # ``.quotes`` (a ``PostgresKalshiBaselineQuoteRepository``) is
        # composed onto the source itself, mirroring ``self._followup.ledger``
        # immediately above -- one engine-bound instance serves both the
        # read-only evidence-source Protocol and the save-side repository
        # Protocol ``SyncKalshiBaselineQuoteEvidence`` needs.
        self._kalshi_baseline_quote_source = PostgresKalshiBaselineQuoteEvidenceSource(engine)
        # Securities Structure Forward Performance v1: reuses the identical
        # ``self._counterfactual_outcome_source`` evidence-source instance
        # immediately above (it already exposes every read this refresh
        # needs -- decisions, observations, assessments, projections-by-id;
        # see ``application.securities_structure_outcome``'s own module
        # docstring for why no new source Protocol was needed), paired with
        # its own new, distinct repository/table.
        self._securities_structure_outcome = PostgresSecuritiesStructureOutcomeRepository(engine)
        # Recurring Strategy PAPER Orchestration v1: the canonical Live
        # Market Observation substrate (Phase C) -- read-only from this
        # executor's own point of view; nothing here ever runs the separate
        # live-observer worker or captures a fresh observation itself.
        self._live_observation = PostgresLiveMarketObservationRepository(engine)
        # Strategy Evaluation History & Promotion Lineage v1: durable
        # retention of this task's own real per-cycle evaluation result for
        # each recurring-catalog candidate -- see ``_execute_strategy_paper_
        # trial``'s own docstring for where it is written, and ``domain.
        # strategy_evaluation_history`` for the full rationale. Read-only
        # from every OTHER task's point of view; this executor never
        # constructs ``application.strategy_evaluation_history.
        # PromoteInternalStrategy`` (promotion stays an explicit, separate,
        # operator-invoked CLI action -- see that class's own docstring).
        self._internal_strategy_evaluations = PostgresInternalStrategyEvaluationRepository(engine)
        # Repeatable Strategy PAPER Evidence v1: durable, replay-safe
        # bookkeeping for bounded, repeatable trial generations -- see
        # ``domain.strategy_paper_trial_lifecycle`` for the full rationale.
        # Read-write from this executor (the only writer of either table;
        # see ``application.strategy_paper_trial_lifecycle``'s own module
        # docstring for why neither repository is ever handed to a
        # promotion-style, operator-only action -- there isn't one here).
        self._strategy_paper_trials = PostgresStrategyPaperTrialRepository(engine)
        self._strategy_provenance = PostgresStrategyVersionProvenanceRepository(engine)
        self._strategy_paper_trial_closures = PostgresStrategyPaperTrialClosureRepository(engine)
        # Strategy PAPER Trial Outcome Assessment v1: the only writer of
        # ``strategy_trial_outcome_assessment``. Nothing in the PAPER
        # lifecycle above reads it.
        self._strategy_trial_outcomes = PostgresStrategyTrialOutcomeAssessmentRepository(engine)
        # Kalshi Settlement & Resolution Evidence Maturation v1: durable,
        # cross-cycle-persistent resolution-fact evidence -- injected into
        # ``SyncFollowUpObservations`` below so the unattended recurring-
        # cycle worker (a fresh process every tick) genuinely avoids
        # re-polling an already-``FINALIZED`` Kalshi market on a later
        # cycle, not merely within one process's lifetime (see
        # ``SyncFollowUpObservations.__init__``'s own docstring for why
        # this is the one dependency that must always be real here).
        self._kalshi_resolution_facts = PostgresKalshiResolutionFactRepository(engine)

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        if task.task_type is OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT:
            # Dispatched before any retained-state snapshot is taken, so an
            # unrelated state-source failure can never block it either.
            return self._execute_strategy_trial_outcome_assessment(executed_at=executed_at)

        if task.task_type is OperatingTaskType.EVIDENCE_GAP_RECOVERY:
            # Likewise independent of the retained-state snapshot.
            return self._execute_evidence_gap_recovery(
                cycle_id=cycle_id, as_of=as_of, executed_at=executed_at
            )

        if task.task_type is OperatingTaskType.KALSHI_MARKET_REFRESH:
            from apex.adapters.postgres import (
                postgres_bounded_orderbook_capture_store,
                postgres_public_binary_complement_store,
                postgres_public_market_discovery_store,
                postgres_public_threshold_ladder_store,
            )

            capability = self._kalshi.latest_capability()
            if capability is None or not capability.public_refresh_available:
                raise OperatingCycleError("OPERATING_KALSHI_PUBLIC_CAPABILITY_UNAVAILABLE")
            requests = [0]
            market_client = KalshiPublicMarketDataClient()
            orderbook_client = KalshiPublicOrderbookDataClient()

            class CountingMarketClient:
                def fetch(
                    self,
                    *,
                    status: str,
                    page_size: int,
                    cursor: str | None,
                    timeout_seconds: int,
                    exclude_multivariate: bool,
                ) -> CapturedPublicMarketPage:
                    requests[0] += 1
                    return market_client.fetch(
                        status=status,
                        page_size=page_size,
                        cursor=cursor,
                        timeout_seconds=timeout_seconds,
                        exclude_multivariate=exclude_multivariate,
                    )

            class CountingOrderbookClient:
                def fetch_market(
                    self, *, market_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    requests[0] += 1
                    return orderbook_client.fetch_market(
                        market_ticker=market_ticker,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_event(
                    self, *, event_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    requests[0] += 1
                    return orderbook_client.fetch_event(
                        event_ticker=event_ticker,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_orderbook(
                    self,
                    *,
                    market_ticker: str,
                    maximum_depth: int,
                    timeout_seconds: int,
                ) -> CapturedKalshiPublicResponse:
                    requests[0] += 1
                    return orderbook_client.fetch_orderbook(
                        market_ticker=market_ticker,
                        maximum_depth=maximum_depth,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_series(
                    self, *, series_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    requests[0] += 1
                    return orderbook_client.fetch_series(
                        series_ticker=series_ticker,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_series_fee_changes(
                    self, *, series_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    requests[0] += 1
                    return orderbook_client.fetch_series_fee_changes(
                        series_ticker=series_ticker,
                        timeout_seconds=timeout_seconds,
                    )

            market_request = PublicMarketScanRequest(
                run_key=f"{cycle_id}-kalshi-markets",
                # Matches the sibling KALSHI_RELATED_MARKET_CONSISTENCY_V1
                # threshold-ladder scan's own already-proven-safe discovery
                # page size (scan_public_threshold_ladders production
                # request, same task, same cycle) -- a wider candidate pool
                # for ScanPublicBinaryComplements' activity-ranked selection
                # below to choose from, not a new, unreviewed provider
                # surface.
                page_size=50,
                maximum_pages=1,
                timeout_seconds=10,
            )
            result = ExecuteKalshiCurrentRefresh(
                repository=self._kalshi,
                discovery=DiscoverPublicPredictionMarkets(
                    client=CountingMarketClient(),
                    normalizer=KalshiPublicMarketNormalizer(),
                    store=postgres_public_market_discovery_store(self._engine),
                    clock=lambda: executed_at,
                ),
                complements=ScanPublicBinaryComplements(
                    client=CountingOrderbookClient(),
                    store=postgres_public_binary_complement_store(self._engine),
                    clock=lambda: executed_at,
                ),
                opportunity_source=PostgresKalshiCurrentOpportunitySource(self._engine),
                provider_request_count=lambda: requests[0],
                clock=lambda: executed_at,
            ).execute(
                KalshiCurrentRefreshCommand(
                    refresh_id=f"{cycle_id}-kalshi-refresh",
                    as_of=as_of,
                    evidence_class=KalshiEvidenceClass.GENUINE_PUBLIC,
                    market_request=market_request,
                    complement_request=BinaryComplementScanRequest(
                        market_discovery_run_id=market_request.ingestion_run_id,
                        # BinaryComplementScanRequest's own dataclass default
                        # (unused by production until now); leaves genuine
                        # headroom under the wider discovery pool above for
                        # activity ranking to matter, rather than selecting
                        # every discovered market regardless of rank.
                        market_limit=20,
                        maximum_depth=5,
                        maximum_bundle_quantity=Decimal(10),
                        timeout_seconds=10,
                    ),
                    idempotency_key=f"{cycle_id}-kalshi-refresh-key",
                )
            )
            capture_store = postgres_bounded_orderbook_capture_store(self._engine)
            normalizer = KalshiPublicMarketNormalizer()
            related = ScanPublicThresholdLadders(
                discovery=DiscoverPublicPredictionMarkets(
                    client=CountingMarketClient(),
                    normalizer=normalizer,
                    store=postgres_public_market_discovery_store(self._engine),
                    clock=lambda: executed_at,
                ),
                capture=CaptureBoundedPublicOrderbookSession(
                    client=CountingOrderbookClient(),
                    store=capture_store,
                    wall_clock=lambda: datetime.now(tz=executed_at.tzinfo),
                    monotonic_clock=time.monotonic,
                    sleeper=time.sleep,
                    normalizer=normalizer,
                ),
                client=CountingOrderbookClient(),
                candidate_selector=lambda run_id, limit: capture_store.select_capture_candidates(
                    ingestion_run_id=run_id,
                    limit=limit,
                ),
                store=postgres_public_threshold_ladder_store(self._engine),
                normalizer=normalizer,
                event_normalizer=KalshiPublicEventNormalizer(),
                wall_clock=lambda: datetime.now(tz=executed_at.tzinfo),
                monotonic_clock=time.monotonic,
                sleeper=time.sleep,
            ).scan(
                PublicThresholdLadderScanRequest(
                    scan_id=f"{cycle_id}-kalshi-related-market-v1",
                    event_count=10,
                    # market_limit/maximum_pages: raised from the dataclass's own hard
                    # floor (50/1) toward its already-validated ceiling (300/3 - the
                    # same "three pages and 300 markets" bound docs/KALSHI_PUBLIC_API_
                    # FACTS.md already documents as safe for shadow_operations'
                    # sibling Kalshi discovery). 150/3 keeps page_size == 150 // 3 ==
                    # 50 identical to today's already-proven single-page request size,
                    # just repeated across more pages, tripling the discovery pool
                    # feeding _activity_key's ranking without enlarging any one
                    # request or touching maximum_enrichments' own request/latency cap.
                    maximum_pages=3,
                    market_limit=150,
                    maximum_enrichments=40,
                    maximum_markets_per_event=8,
                    maximum_relationships=100,
                    # rounds intentionally left at 2 (unchanged): checked retained
                    # evidence before touching it (Kalshi Candidate Scarcity /
                    # Discovery Quality v1 Phase 0) - across all 79 relationships ever
                    # evaluated, matched_quantity and both-required-legs-quoted
                    # presence never varied across rounds for the same relationship
                    # (0/79). Re-evaluating an already-selected relationship more
                    # times does not surface a different book state; only changing
                    # which relationships get selected does, so this milestone spends
                    # its bounded change budget there instead.
                    rounds=2,
                    interval_seconds=5,
                    maximum_depth=5,
                    maximum_bundle_quantity=Decimal(10),
                    maximum_observation_skew_seconds=5,
                    timeout_seconds=10,
                )
            )
            opportunities = self._kalshi.opportunities_for_refresh(result.value.refresh_id)
            exhaustive_sets = RetainedSharedOpportunitySource(
                self._engine
            ).exhaustive_event_set_evaluations()
            outputs = (
                result.value.refresh_id,
                *result.value.evaluation_ids,
                *(item.opportunity_id for item in opportunities),
                related.scan.scan_id,
                *(item.evaluation_id for item in related.evaluations),
                *(item.evaluation_id for item in exhaustive_sets),
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if result.idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                outputs,
                outputs if result.idempotent_replay else (),
                not result.idempotent_replay,
                requests[0],
                (
                    *result.value.warnings,
                    f"KALSHI_RELATED_RELATIONSHIPS:{related.scan.relationship_count}",
                    f"KALSHI_RELATED_CANDIDATES:{related.scan.candidate_count}",
                    f"KALSHI_EXHAUSTIVE_EVENT_SETS:{len(exhaustive_sets)}",
                    "KALSHI_EXHAUSTIVE_VERIFIED:"
                    f"{sum(item.exhaustiveness_verified for item in exhaustive_sets)}",
                    "KALSHI_EXHAUSTIVE_SOURCE_ELIGIBLE:"
                    f"{sum(item.source_eligible for item in exhaustive_sets)}",
                    "KALSHI_PAPER_OPERATION_DISABLED_BY_DEFAULT",
                    "KALSHI_LIVE_EXECUTION_UNAVAILABLE",
                ),
            )

        if task.task_type is OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE:
            # Independent of every crypto/Kalshi task above and below --
            # ``task.dependency_types`` is declared empty in ``plan_due_
            # work`` for this task type, and this branch touches none of
            # their repositories. A failure here (any exception) is caught
            # generically by ``ExecuteOperatingCycle`` exactly like every
            # other task's failure, never propagating to crash the cycle.
            #
            # Recurring Operating Health v1: this inner re-derivation is a
            # deliberate defense-in-depth re-check of ``plan_due_work``'s own
            # DUE disposition (see the comment below), so it must be fed the
            # identical true wall-clock instant the planner used --
            # ``executed_at``, never the day-scoped midnight ``as_of``.
            # Passing ``as_of`` here was a genuine bug: midnight is always
            # before today's own capture window, so this check would
            # unconditionally disagree with a genuinely DUE planning decision
            # and raise below, meaning the options task could never actually
            # reach ``COMPLETED`` through the real worker.
            options_plan, window_policy, calendar_sessions, due_decision = (
                resolve_options_capture_due_decision(
                    quote_repository=self._options_quote,
                    checkpoint_repository=self._options_checkpoints,
                    as_of=executed_at,
                )
            )
            if options_plan is None:
                raise OperatingCycleError("OPERATING_OPTIONS_CAPTURE_PLAN_MISSING")
            if due_decision.status is not OptionsCaptureDueStatus.DUE:
                # ``plan_due_work`` already gates execution on ``DueTask.
                # disposition``, so reaching here while genuinely not due
                # would itself be a planning bug -- fail loudly rather than
                # silently no-op or fabricate a capture.
                raise OperatingCycleError("OPERATING_OPTIONS_CAPTURE_NOT_GENUINELY_DUE")

            # Stable cost/structure-economics policy across sessions
            # (milestone section 17): reuse whatever this plan already has
            # retained; only this plan's genuinely first invocation
            # declares the defaults. Never re-tuned day to day.
            pricing_policy = self._options_structures.latest_pricing_policy()
            if pricing_policy is None:
                pricing_policy = CreateStructurePricingPolicy(self._options_structures).execute(
                    policy=StructurePricingPolicy.create()
                )
            fee_policy = self._options_structures.latest_fee_policy()
            if fee_policy is None:
                fee_policy = CreateOptionsFeePolicy(self._options_structures).execute(
                    policy=OptionsFeePolicy.create_alpaca_regulatory_evidenced_2026_04_01()
                )
            slippage_policy = self._options_structures.latest_slippage_policy()
            if slippage_policy is None:
                slippage_policy = CreateOptionsSlippagePolicy(self._options_structures).execute(
                    policy=OptionsSlippagePolicy.create()
                )
            structure_policy = self._options_structures.latest_structure_policy()
            if structure_policy is None:
                structure_policy = CreateOptionsStructureResearchPolicy(
                    self._options_structures
                ).execute(
                    policy=OptionsStructureResearchPolicy.create(
                        pricing_policy=pricing_policy,
                        fee_policy=fee_policy,
                        slippage_policy=slippage_policy,
                        created_at=executed_at,
                    )
                )

            rv_resolver = ResolveRealizedVolatility(
                historical_repository=PostgresHistoricalMarketDataRepository(self._engine),
                evidence=PostgresSecuritiesRepository(self._engine),
                cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            )
            capture = AlpacaOptionsResearchEvidenceCapture(
                quote_repository=self._options_quote,
                structures_repository=self._options_structures,
                checkpoint_repository=self._options_checkpoints,
                rv_resolver=rv_resolver,
                capture_client=self._options_capture_client,
            )
            try:
                outcome = capture.run(
                    OptionsResearchEvidenceCaptureCommand(
                        plan=options_plan,
                        due=due_decision,
                        window_policy=window_policy,
                        sessions=calendar_sessions,
                        coverage_policy=ChainCoverageQualityPolicy.create(),
                        structure_policy=structure_policy,
                        fee_policy=fee_policy,
                        slippage_policy=slippage_policy,
                        feed_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
                        now=executed_at,
                    )
                )
            except (OSError, RuntimeError, ValueError) as error:
                # Genuine provider reads can have already happened before a
                # later step (e.g. snapshot persistence) fails -- the
                # exact 2026-08-12 shape this milestone fixes the FK gap
                # for. ``capture.provider_requests_so_far`` is instance
                # state, so it survives the exception unwind that would
                # otherwise lose ``run``'s own local counter. Annotating
                # (never suppressing/converting) the same propagating
                # exception lets ``ExecuteOperatingCycle._dispatch_task``'s
                # generic failure handler report the genuine count instead
                # of hardcoding zero -- see Options Automatic Capture
                # Parent Persistence & Partial-Evidence Recovery v1, Part
                # 12. ``getattr(..., 0)`` there means every other task
                # type's failure accounting is completely unchanged.
                error.network_requests_before_failure = (  # type: ignore[union-attr]
                    capture.provider_requests_so_far
                )
                raise
            outputs = (
                outcome.checkpoint.checkpoint_id,
                *outcome.checkpoint.fresh_snapshot_ids,
                *outcome.checkpoint.structure_snapshot_ids,
            )
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                outputs,
                (),
                True,
                outcome.provider_request_count,
                (
                    f"OPTIONS_UNDERLYINGS_CAPTURED:{outcome.checkpoint.underlyings_captured_count}",
                    f"OPTIONS_STRUCTURES_RETAINED:{outcome.checkpoint.structures_retained}",
                    "OPTIONS_PAPER_AND_LIVE_EXECUTION_REMAIN_DISABLED",
                ),
            )

        if task.task_type is OperatingTaskType.CRYPTO_UNIVERSE_REFRESH:
            # Composes the existing, already-genuine crypto-universe capture
            # pipeline (identical services the manual ``crypto-products-capture``
            # / ``crypto-observations-capture`` / ``crypto-universe-materialize``
            # CLI commands already exercise -- see ``application.crypto_universe``)
            # into one bounded recurring step. Deliberately excludes
            # ``RunCryptoDiscovery``: this task refreshes *source availability*
            # (product directory + eligibility) only, never strategy/ranking
            # output -- see AGENTS.md's "Crypto Universe Recurring Refresh &
            # Dependency Health v1" note on scope.
            universe_source = CoinbasePublicUniverseSource()
            capture_id = f"{cycle_id}-crypto-universe-capture"
            snapshot_id = f"{cycle_id}-crypto-universe-snapshot"
            # Pre-check *before* each call, exactly like the existing
            # ``crypto-observations-capture`` CLI handler does, so a genuinely
            # honest ``network_requests`` count survives crash-recovery replay:
            # ``CapturePublicCryptoProducts``/``CaptureCryptoUniverseObservations``
            # are each independently idempotent per already-retained-row checks,
            # so calling both unconditionally is always safe -- only the
            # before/after comparison tells us whether *this* call genuinely
            # reached the provider.
            prior_capture = self._crypto.product_capture(capture_id)
            product_capture = CapturePublicCryptoProducts(self._crypto, universe_source).execute(
                capture_id=capture_id,
                requested_at=executed_at,
            )
            completed_day = completed_utc_day(as_of)
            before_observation_ids = {
                item.observation_id
                for item in self._crypto.observations()
                if item.source_capture_id == capture_id
            }
            observations = CaptureCryptoUniverseObservations(self._crypto, universe_source).execute(
                capture_id=capture_id,
                # ``CaptureCryptoUniverseObservations`` (unlike the separate,
                # already-eligible-universe-only ``CaptureCryptoCalibrationHistory``
                # CRYPTO_MARKET_PULSE composes) issues exactly one Coinbase
                # candles request per shortlisted product, and that provider
                # endpoint accepts at most ``MAX_CANDLES_PER_REQUEST`` days per
                # request -- so the widest single-request window is used here
                # for the most margin over the domain's own
                # ``MIN_HISTORY_BUCKETS`` (170-day) eligibility floor.
                interval_start=completed_day - timedelta(days=MAX_CANDLES_PER_REQUEST),
                interval_end=completed_day,
                requested_at=executed_at,
            )
            new_observation_count = sum(
                1 for item in observations if item.observation_id not in before_observation_ids
            )
            universe_snapshot = MaterializeCryptoUniverse(self._crypto).execute(
                snapshot_id=snapshot_id,
                capture_id=capture_id,
                created_at=executed_at,
            )
            catalog_requests = 0 if prior_capture is not None else product_capture.request_count
            total_requests = catalog_requests + new_observation_count
            idempotent_replay = total_requests == 0
            return TaskExecution(
                OperatingTaskStatus.REUSED if idempotent_replay else OperatingTaskStatus.COMPLETED,
                (product_capture.capture_id, universe_snapshot.snapshot_id),
                (
                    (product_capture.capture_id, universe_snapshot.snapshot_id)
                    if idempotent_replay
                    else ()
                ),
                not idempotent_replay,
                total_requests,
                (
                    f"CRYPTO_UNIVERSE_ELIGIBLE_PRODUCTS:{len(universe_snapshot.eligible_product_ids)}",
                    "CRYPTO_UNIVERSE_CONSIDERED_PRODUCTS:"
                    f"{len(universe_snapshot.considered_product_ids)}",
                ),
            )

        if task.task_type is OperatingTaskType.CRYPTO_MARKET_PULSE:
            pulse_plan = self._crypto.refresh_plan(plan.crypto_pulse_plan_id)
            if pulse_plan is None:
                raise OperatingCycleError("OPERATING_CRYPTO_PULSE_PLAN_MISSING")
            snapshot = self._crypto.universe_snapshot(pulse_plan.universe_snapshot_id)
            if snapshot is None:
                raise OperatingCycleError("OPERATING_CRYPTO_UNIVERSE_MISSING")
            completed_day = completed_utc_day(as_of)
            retained_history = tuple(
                item
                for item in self._crypto.observations()
                if item.product_id in snapshot.eligible_product_ids
                and item.source_capture_id == snapshot.source_capture_id
                and item.evidence_class is snapshot.evidence_class
                and item.coverage_partitions
                and len(item.candles) >= 365
                and item.interval_end <= completed_day
                and item.interval_end >= completed_day - timedelta(days=7)
            )
            history_requests = 0
            history_outputs: tuple[str, ...]
            history_warning: str
            if {item.product_id for item in retained_history} == set(snapshot.eligible_product_ids):
                selected_history = tuple(
                    max(
                        (item for item in retained_history if item.product_id == product_id),
                        key=lambda item: (
                            item.interval_end,
                            len(item.candles),
                            item.observation_id,
                        ),
                    )
                    for product_id in snapshot.eligible_product_ids
                )
                history_outputs = tuple(item.observation_id for item in selected_history)
                history_warning = "CRYPTO_CALIBRATION_HISTORY_REUSED"
            else:
                history_result = CaptureCryptoCalibrationHistory(
                    self._crypto, CoinbasePublicUniverseSource()
                ).execute(
                    history_id=(
                        f"{snapshot.snapshot_id}-calibration-{completed_day.strftime('%Y%m%d')}-v1"
                    ),
                    universe_snapshot_id=snapshot.snapshot_id,
                    interval_start=completed_day - timedelta(days=730),
                    interval_end=completed_day,
                    requested_at=executed_at,
                )
                history_requests = history_result.request_count
                history_outputs = tuple(item.observation_id for item in history_result.observations)
                history_warning = (
                    "CRYPTO_CALIBRATION_HISTORY_REUSED"
                    if history_result.idempotent_replay
                    else "CRYPTO_CALIBRATION_HISTORY_CAPTURED"
                )
            pulse_result = ExecuteCryptoMarketPulseRefresh(
                self._crypto, CoinbasePublicUniverseSource()
            ).execute(
                refresh_id=f"{cycle_id}-crypto-pulse",
                plan_id=plan.crypto_pulse_plan_id,
                as_of=as_of,
                requested_at=executed_at,
            )
            outputs = tuple(
                value
                for value in (
                    pulse_result.refresh.refresh_id,
                    None if pulse_result.pulse is None else pulse_result.pulse.pulse_id,
                    (None if pulse_result.summary is None else pulse_result.summary.summary_id),
                )
                if value is not None
            )
            if pulse_result.state is RefreshState.FAILED:
                return TaskExecution(
                    OperatingTaskStatus.FAILED,
                    outputs,
                    (),
                    bool(outputs),
                    pulse_result.network_requests_performed,
                    pulse_result.failures,
                    "CRYPTO_PULSE_REFRESH_FAILED",
                    True,
                )
            if pulse_result.pulse is None:
                raise OperatingCycleError("OPERATING_CRYPTO_PULSE_OUTPUT_MISSING")
            current_result = RunCurrentCryptoRelativeStrength(self._crypto).execute(
                run_id=f"{cycle_id}-crypto-relative-strength-v1",
                pulse_id=pulse_result.pulse.pulse_id,
                generated_at=executed_at,
            )
            reversal_result = RunCurrentCryptoReversalRegime(self._crypto).execute(
                run_id=f"{cycle_id}-crypto-reversal-regime-v1",
                pulse_id=pulse_result.pulse.pulse_id,
                generated_at=executed_at,
            )
            stateful_outputs: tuple[str, ...] = ()
            stateful_warning = "CRYPTO_STATEFUL_PULLBACK_ROTATION_NOT_IMPLEMENTED"
            specifications = self._crypto_stateful.specifications()
            experiments = self._crypto_stateful.experiment_runs()
            if specifications and experiments:
                experiment = experiments[-1]
                recommendation = self._research_operations.recommendation_for_run(experiment.run_id)
                if recommendation is not None:
                    inputs = current_crypto_method_inputs(
                        self._crypto,
                        pulse=pulse_result.pulse,
                        generated_at=executed_at,
                    )
                    signal, stateful_opportunities, _ = RunCurrentCryptoStatefulMethod(
                        self._crypto_stateful, self._crypto
                    ).execute(
                        signal_id=f"{cycle_id}-crypto-stateful-pullback-rotation-v1",
                        specification=specifications[-1],
                        experiment=experiment,
                        recommendation_id=recommendation.recommendation_id,
                        source_pulse_id=pulse_result.pulse.pulse_id,
                        universe_snapshot_id=inputs.snapshot.snapshot_id,
                        observations=inputs.observations,
                        current_market=inputs.market,
                        generated_at=executed_at,
                    )
                    stateful_outputs = (
                        signal.signal_id,
                        *(value.opportunity_id for value in stateful_opportunities),
                    )
                    stateful_warning = f"CRYPTO_STATEFUL_CURRENT_STATE:{signal.state.value}"
            outputs = (
                *outputs,
                *history_outputs,
                current_result.run.run_id,
                *(item.opportunity_id for item in current_result.opportunities),
                reversal_result.run.run_id,
                *(item.opportunity_id for item in reversal_result.opportunities),
                *stateful_outputs,
            )
            public_book_count = sum(item.best_bid is not None for item in pulse_result.checkpoints)
            source_rejected_count = sum(
                item.status.value == "REJECTED" for item in current_result.opportunities
            )
            reversal_rejected_count = sum(
                item.status.value == "REJECTED" for item in reversal_result.opportunities
            )
            # A checkpoint built against a plan.version < 2 refresh never
            # attempts book capture at all (see
            # ExecuteCryptoMarketPulseRefresh.execute), so its quality_reasons
            # carry no signal explaining PUBLIC_BOOK_CHECKPOINTS:0 -- the
            # absence looks identical to "provider returned nothing usable".
            # Make the exact, distinguishable cause visible on the retained
            # task result itself rather than requiring a source read: either
            # the bound pulse plan never requested book evidence, or it did
            # and specific checkpoints rejected it for named reasons.
            if pulse_plan.version < 2:
                book_capture_diagnostic = (
                    "PUBLIC_BOOK_CAPTURE_NOT_REQUESTED_BY_BOUND_PULSE_PLAN:"
                    f"{pulse_plan.plan_id}:v{pulse_plan.version}"
                )
            else:
                rejected_book_reasons = sorted(
                    {
                        reason
                        for item in pulse_result.checkpoints
                        for reason in item.quality_reasons
                        if reason.startswith("PUBLIC_ORDER_BOOK")
                    }
                )
                book_capture_diagnostic = (
                    "PUBLIC_BOOK_CAPTURE_REJECTED:" + ",".join(rejected_book_reasons)
                    if rejected_book_reasons
                    else "PUBLIC_BOOK_CAPTURE_ATTEMPTED_AND_USABLE"
                    if public_book_count
                    else "PUBLIC_BOOK_CAPTURE_ATTEMPTED_NO_USABLE_BOOK"
                )
            return TaskExecution(
                OperatingTaskStatus.COMPLETED,
                outputs,
                (),
                True,
                history_requests + pulse_result.network_requests_performed,
                (
                    *pulse_result.failures,
                    history_warning,
                    f"PUBLIC_BOOK_CHECKPOINTS:{public_book_count}",
                    book_capture_diagnostic,
                    f"CRYPTO_SOURCE_REJECTED:{source_rejected_count}",
                    f"CRYPTO_REVERSAL_REGIME_REJECTED:{reversal_rejected_count}",
                    stateful_warning,
                    "CRYPTO_CURRENT_STRATEGY_RETAINED_EVIDENCE_ONLY",
                ),
            )

        state = self._state_source.snapshot(as_of=as_of, evaluated_at=executed_at)
        if task.task_type is OperatingTaskType.TOTAL_MARKET_CONTEXT_REFRESH:
            if state.securities_batch_id is None:
                raise OperatingCycleError("OPERATING_SECURITIES_BATCH_MISSING")
            context_result = MaterializeTotalMarketContextSnapshot(
                self._context, RetainedTotalMarketContextSource(self._engine)
            ).execute(
                snapshot_id=f"{cycle_id}-total-market-context",
                source_batch_id=state.securities_batch_id,
                as_of=as_of,
                idempotency_key=f"{cycle_id}-total-market-context-key",
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if context_result.idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                (context_result.snapshot.snapshot_id,),
                (
                    (context_result.snapshot.snapshot_id,)
                    if context_result.idempotent_replay
                    else ()
                ),
                not context_result.idempotent_replay,
                0,
            )

        if task.task_type is OperatingTaskType.SHARED_OPPORTUNITY_REBUILD:
            if state.crypto_pulse_id is None:
                raise OperatingCycleError("OPERATING_CRYPTO_PULSE_MISSING")
            shared_result = MaterializeSharedOpportunitySet(
                self._shared, RetainedSharedOpportunitySource(self._engine)
            ).execute(
                opportunity_set_id=f"{cycle_id}-shared-opportunity",
                crypto_pulse_id=state.crypto_pulse_id,
                kalshi_evaluation_ids=state.kalshi_evaluation_ids,
                as_of=as_of,
                idempotency_key=f"{cycle_id}-shared-opportunity-key",
            )
            state_counts = {
                state: sum(item.state.value == state for item in shared_result.decisions)
                for state in (
                    "ELIGIBLE",
                    "BLOCKED_MISSING_EVIDENCE",
                    "REJECTED",
                    "EXPIRED",
                )
            }
            # The umbrella SHARED_BLOCKED_MISSING_EVIDENCE / SHARED_REJECTED
            # counts above intentionally stay as top-level summaries, but the
            # exact canonical reason behind each non-eligible projection must
            # remain visible without a separate diagnosis session: break every
            # retained EligibilityDecision.reasons entry down per venue so the
            # same warning_codes surface already read by the CLI
            # (operating-task-inspect), Operator API, and Operator Console
            # carries it -- no new table or dashboard.
            venue_by_projection = {
                item.projection_id: item.venue for item in shared_result.projections
            }
            reason_counts: Counter[tuple[str, str]] = Counter()
            for decision in shared_result.decisions:
                if decision.state.value not in ("BLOCKED_MISSING_EVIDENCE", "REJECTED"):
                    continue
                venue = venue_by_projection.get(decision.projection_id)
                venue_label = venue.value if venue is not None else "UNKNOWN"
                for reason in decision.reasons:
                    reason_counts[(venue_label, reason)] += 1
            reason_warnings = tuple(
                f"{venue_label}_BLOCKER:{reason}:{count}"
                for (venue_label, reason), count in sorted(reason_counts.items())
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if shared_result.idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                (shared_result.opportunity_set.opportunity_set_id,),
                (
                    (shared_result.opportunity_set.opportunity_set_id,)
                    if shared_result.idempotent_replay
                    else ()
                ),
                not shared_result.idempotent_replay,
                0,
                (
                    f"SHARED_ELIGIBLE:{state_counts['ELIGIBLE']}",
                    (f"SHARED_BLOCKED_MISSING_EVIDENCE:{state_counts['BLOCKED_MISSING_EVIDENCE']}"),
                    f"SHARED_REJECTED:{state_counts['REJECTED']}",
                    f"SHARED_EXPIRED:{state_counts['EXPIRED']}",
                    "NO_AUTOMATIC_PAPER_ACTION",
                    *reason_warnings,
                ),
            )

        if task.task_type is OperatingTaskType.CONTEXT_ENRICHMENT_REBUILD:
            if state.shared_set_id is None or state.total_market_snapshot_id is None:
                raise OperatingCycleError("OPERATING_CONTEXT_INPUT_MISSING")
            enrichment_result = AttachTotalMarketContext(self._context).execute(
                enriched_set_id=f"{cycle_id}-context-enriched",
                opportunity_set_id=state.shared_set_id,
                context_snapshot_id=state.total_market_snapshot_id,
                attached_at=as_of,
                idempotency_key=f"{cycle_id}-context-enriched-key",
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if enrichment_result.idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                (enrichment_result.enriched_set.enriched_set_id,),
                (
                    (enrichment_result.enriched_set.enriched_set_id,)
                    if enrichment_result.idempotent_replay
                    else ()
                ),
                not enrichment_result.idempotent_replay,
                0,
            )

        if task.task_type is OperatingTaskType.RESEARCH_ALLOCATION_REBUILD:
            if state.enriched_set_id is None:
                raise OperatingCycleError("OPERATING_CONTEXT_ENRICHED_SET_MISSING")
            retained = self._cross_venue.decision_for_enriched_set(state.enriched_set_id)
            if retained is not None:
                return TaskExecution(
                    OperatingTaskStatus.REUSED,
                    (retained.decision_id,),
                    (retained.decision_id,),
                    False,
                    0,
                    (
                        "CROSS_VENUE_ALLOCATION_INPUTS_UNCHANGED",
                        f"PAPER_TRIAL_PROPOSALS:{len(retained.paper_trial_proposal_ids)}",
                        f"RETAINED_CASH:{retained.retained_cash}",
                    ),
                )
            allocation_result = MaterializeCrossVenueAllocation(
                self._cross_venue, self._cross_venue_source
            ).execute(
                decision_id=f"{cycle_id}-cross-venue-allocation",
                enriched_set_id=state.enriched_set_id,
                decided_at=as_of,
                idempotency_key=f"{cycle_id}-cross-venue-allocation-key",
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if allocation_result.idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                (allocation_result.decision.decision_id,),
                (
                    (allocation_result.decision.decision_id,)
                    if allocation_result.idempotent_replay
                    else ()
                ),
                not allocation_result.idempotent_replay,
                0,
                (
                    f"PAPER_TRIAL_PROPOSALS:{len(allocation_result.proposals)}",
                    f"RETAINED_CASH:{allocation_result.decision.retained_cash}",
                    "NO_PAPER_AUTHORIZATION_OR_ORDER_CREATED",
                ),
            )

        if task.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION:
            return self._execute_crypto_paper_trial(
                plan=plan, cycle_id=cycle_id, as_of=as_of, executed_at=executed_at, state=state
            )

        if task.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION:
            return self._execute_kalshi_paper_trial(
                plan=plan, cycle_id=cycle_id, as_of=as_of, executed_at=executed_at, state=state
            )

        if task.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION:
            return self._execute_strategy_paper_trial(
                plan=plan, cycle_id=cycle_id, as_of=as_of, executed_at=executed_at, state=state
            )

        if task.task_type is OperatingTaskType.PORTFOLIO_SNAPSHOT_REFRESH:
            # Unconditionally due every cycle (see ``plan_due_work``); scoped
            # today to exactly the two PAPER accounts this same executor
            # already manages (crypto/Kalshi). A genuinely missing account
            # (e.g. the very first cycle before either PAPER account has
            # ever been bootstrapped) is not an error: ``BuildPortfolioSnapshot``
            # records it as an honest ``UnavailablePortfolioSource`` rather
            # than failing the task. Securities internal PAPER and genuine
            # External PAPER (Alpaca) evidence are deliberately out of scope
            # here -- neither has a "the" campaign/account id the recurring
            # cycle itself already tracks, and Alpaca requires a separately
            # governed live provider read this cycle has never performed.
            snapshot_id = f"{cycle_id}-portfolio-snapshot"
            build_result = BuildPortfolioSnapshot(self._portfolio, self._portfolio_source).execute(
                snapshot_id=snapshot_id,
                as_of=as_of,
                created_at=executed_at,
                idempotency_key=f"{snapshot_id}-key",
                selection=PortfolioSourceSelection(
                    # Recurring Strategy PAPER Orchestration v1 added the
                    # second crypto account id -- portfolio/mandate
                    # visibility now honestly includes the new strategy-
                    # driven capital pool too (read-only; grants no new
                    # authority -- evaluate_mandate remains fail-closed).
                    crypto_account_ids=(
                        GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
                        GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                    ),
                    kalshi_account_ids=(GENUINE_KALSHI_PAPER_ACCOUNT_ID,),
                ),
            )
            snapshot_outputs = (
                build_result.snapshot.snapshot_id,
                *(item.balance_id for item in build_result.account_balances),
                *(item.position_id for item in build_result.positions),
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if build_result.idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                snapshot_outputs,
                snapshot_outputs if build_result.idempotent_replay else (),
                not build_result.idempotent_replay,
                0,
                (
                    f"PORTFOLIO_COMPLETENESS:{build_result.snapshot.completeness.value}",
                    f"PORTFOLIO_UNAVAILABLE_SOURCES:{len(build_result.snapshot.unavailable_sources)}",
                    "PORTFOLIO_SCOPE_CRYPTO_AND_KALSHI_PAPER_ONLY",
                    "NO_SECURITIES_CAMPAIGN_OR_EXTERNAL_PAPER_IN_RECURRING_SCOPE",
                ),
            )

        if task.task_type is OperatingTaskType.MANDATE_EVALUATION_REBUILD:
            # ``plan_due_work`` already gates execution on an active mandate
            # and a retained Shared Opportunity set both existing, so
            # reaching either guard below would itself be a planning bug --
            # fail loudly rather than silently no-op or evaluate against a
            # stale/wrong mandate, exactly like the options capture task.
            if state.active_mandate_id is None:
                raise OperatingCycleError("OPERATING_MANDATE_EVALUATION_NO_ACTIVE_MANDATE")
            if state.shared_set_id is None:
                raise OperatingCycleError("OPERATING_MANDATE_EVALUATION_SHARED_SET_MISSING")
            lineage_id = state.active_mandate_lineage_id or DEFAULT_MANDATE_LINEAGE_ID
            decisions = self._shared.eligibility_decisions(state.shared_set_id)
            eligible_projection_ids = tuple(
                sorted(
                    item.projection_id
                    for item in decisions
                    if item.state is EligibilityState.ELIGIBLE
                )
            )
            evaluation_ids: list[str] = []
            replayed_evaluation_ids: list[str] = []
            mandate_state_counts: dict[str, int] = {"ALLOWED": 0, "CONSTRAINED": 0, "REJECTED": 0}
            unknown_facts: set[str] = set()
            for projection_id in eligible_projection_ids:
                # Unlike every sibling idempotency key in this executor
                # (``cycle_id`` + a short fixed literal), this task loops
                # per-opportunity and must fold in a second, independently
                # unbounded identifier -- a retained ``projection_id`` is
                # legitimately allowed up to 300 chars by
                # ``shared_opportunity``'s own ``_identity`` contract, and
                # today's real "shared-crypto-current-..."/"shared-crypto-
                # ..." projections already run 118-171 chars. Splicing the
                # raw strings together (as before) has no length ceiling and
                # blew past ``mandate_evaluation.idempotency_key``'s
                # deliberate, repo-wide ``VARCHAR(160)`` budget the first
                # time a genuinely-ELIGIBLE long-id opportunity reached this
                # task. ``deterministic_id`` is the same bounded-derived-id
                # mechanism already used everywhere else in this codebase
                # (``evaluation_id``, ``mandate_id``, ...) for exactly this:
                # collapsing arbitrary-length content into one short, stable
                # identifier -- same inputs always produce the same key, so
                # replay/idempotency semantics are unchanged.
                idempotency_key = deterministic_id(
                    "mandate_evaluation_task_replay", (cycle_id, projection_id)
                )
                eval_result = EvaluateOpportunityAgainstMandate(self._mandate).execute(
                    idempotency_key=idempotency_key,
                    opportunity_id=projection_id,
                    evaluated_at=executed_at,
                    mandate_lineage_id=lineage_id,
                )
                evaluation_ids.append(eval_result.evaluation.evaluation_id)
                if eval_result.idempotent_replay:
                    replayed_evaluation_ids.append(eval_result.evaluation.evaluation_id)
                mandate_state_counts[eval_result.evaluation.state.value] += 1
                unknown_facts.update(eval_result.evaluation.unknown_facts)
            mandate_idempotent_replay = len(replayed_evaluation_ids) == len(evaluation_ids)
            return TaskExecution(
                OperatingTaskStatus.REUSED
                if mandate_idempotent_replay
                else OperatingTaskStatus.COMPLETED,
                tuple(evaluation_ids),
                tuple(replayed_evaluation_ids),
                not mandate_idempotent_replay,
                0,
                (
                    f"MANDATE_ACTIVE_LINEAGE_VERSION:{lineage_id}:{state.active_mandate_version}",
                    f"MANDATE_ELIGIBLE_OPPORTUNITIES_EVALUATED:{len(eligible_projection_ids)}",
                    f"MANDATE_ALLOWED:{mandate_state_counts['ALLOWED']}",
                    f"MANDATE_CONSTRAINED:{mandate_state_counts['CONSTRAINED']}",
                    f"MANDATE_REJECTED:{mandate_state_counts['REJECTED']}",
                    *(f"MANDATE_UNKNOWN_FACT:{item}" for item in sorted(unknown_facts)),
                    "NO_PAPER_AUTHORIZATION_RESERVATION_OR_ORDER_CREATED",
                ),
            )

        if task.task_type is OperatingTaskType.PERFORMANCE_ATTRIBUTION_REBUILD:
            attribution_result = MaterializePerformanceAttribution(
                self._performance, self._performance_source
            ).execute(
                run_id=f"{cycle_id}-performance",
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                materialized_at=as_of,
                idempotency_key=f"{cycle_id}-performance-attribution-key",
            )
            health_result = MaterializeStrategyHealth(self._performance).execute(
                run_id=f"{cycle_id}-performance",
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                as_of=as_of,
                idempotency_key=f"{cycle_id}-strategy-health-key",
            )
            outputs = (
                *(item.attribution_id for item in attribution_result.attributions),
                *(item.health_id for item in health_result.assessments),
                *(item.summary_id for item in health_result.method_summaries),
                *(item.summary_id for item in health_result.venue_summaries),
                health_result.portfolio_summary.summary_id,
            )
            idempotent_replay = (
                len(attribution_result.replayed_attribution_ids)
                == len(attribution_result.attributions)
                and len(health_result.replayed_ids)
                == len(health_result.assessments)
                + len(health_result.method_summaries)
                + len(health_result.venue_summaries)
                + 1
            )
            return TaskExecution(
                OperatingTaskStatus.REUSED if idempotent_replay else OperatingTaskStatus.COMPLETED,
                outputs,
                (*attribution_result.replayed_attribution_ids, *health_result.replayed_ids),
                not idempotent_replay,
                0,
                (
                    *health_result.operator_attention_items,
                    "ATTRIBUTION_USED_RETAINED_EVIDENCE_ONLY",
                    "NO_PROVIDER_BROKER_OR_FINANCIAL_MUTATION",
                ),
            )

        if task.task_type is OperatingTaskType.RESEARCH_OPERATIONS_REFRESH:
            facts = self._research_agenda_source.facts(as_of=as_of)
            # Counterfactual V2 Research Evidence Quarantine v1: read for
            # disclosure only. ``facts()`` has already withheld these
            # cohorts; reporting the count here is what keeps a withheld
            # cohort visible as a deliberate, explainable decision on the
            # Operations page rather than as evidence that silently stopped
            # producing agenda items one day for no stated reason.
            quarantined = self._research_agenda_source.quarantined_counterfactual_outcome_cohorts()
            refresh = RefreshResearchAgenda(self._research_operations).execute(facts=facts)
            generated = GenerateResearchProposals(
                self._research_operations,
                PostgresRetainedResearchKnowledgeSource(self._engine),
            ).execute(
                generator_run_id=f"generator-{cycle_id}",
                generated_at=as_of,
                actor="system:operating-cycle",
                context=self._research_agenda_source.generation_context(
                    agenda_items=tuple(refresh.items)
                ),
            )
            awaiting = tuple(
                value.proposal_id
                for value in self._research_operations.proposals()
                if proposal_state(self._research_operations, value.proposal_id)
                is ResearchProposalState.READY_FOR_REVIEW
            )
            approved = tuple(
                value.proposal_id
                for value in self._research_operations.proposals()
                if proposal_state(self._research_operations, value.proposal_id)
                is ResearchProposalState.APPROVED_FOR_EXPERIMENT
            )
            outputs = (
                *(value.agenda_item_id for value in refresh.items),
                generated.generator_run.generator_run_id,
                *(value.proposal_id for value in generated.proposals),
            )
            method_run_id: str | None = None
            if self._research_operations.first_method_implementation() is not None:
                method_run, _ = MaterializeFirstGeneratedResearchMethodRun(
                    self._research_operations,
                    PostgresCurrentOpportunityOperationalEvidenceSource(self._engine),
                ).execute(
                    method_run_id=f"first-generated-method-{cycle_id}",
                    completed_at=executed_at,
                )
                method_run_id = method_run.method_run_id
                outputs = (*outputs, method_run_id)
            return TaskExecution(
                (
                    OperatingTaskStatus.REUSED
                    if len(refresh.replayed_item_ids) == len(refresh.items)
                    else OperatingTaskStatus.COMPLETED
                ),
                outputs,
                refresh.replayed_item_ids,
                len(refresh.replayed_item_ids) != len(refresh.items),
                0,
                (
                    f"RESEARCH_AGENDA_ITEMS:{len(refresh.items)}",
                    f"COUNTERFACTUAL_COHORTS_QUARANTINED_FROM_NEW_RESEARCH:"
                    f"{sum(1 for item in quarantined if not item.sourcing_selection)}",
                    # Counterfactual Outcome Evidence Specificity v3: V3
                    # DECISION_TIME cohorts are withheld by a deliberate
                    # sourcing selection, not a defect -- counted apart so the
                    # Operations page never reads them as one.
                    f"COUNTERFACTUAL_V3_DECISION_TIME_COHORTS_WITHHELD:"
                    f"{sum(1 for item in quarantined if item.sourcing_selection)}",
                    f"RESEARCH_PROPOSALS_GENERATED:{len(generated.proposals)}",
                    f"RESEARCH_REVIEW_QUEUE_REBUILT:{len(generated.priorities)}",
                    f"RESEARCH_PROPOSALS_AWAITING_REVIEW:{len(awaiting)}",
                    f"APPROVED_EXPERIMENTS_NOT_AUTOMATICALLY_RUN:{len(approved)}",
                    f"FIRST_GENERATED_METHOD_REFRESH:{method_run_id or 'NOT_IMPLEMENTED'}",
                    "NO_AUTOMATIC_RESEARCH_PROPOSAL_APPROVAL",
                    "NO_AUTOMATIC_EXPERIMENT_EXECUTION",
                    "NO_PROVIDER_BROKER_OR_FINANCIAL_MUTATION",
                ),
            )

        if task.task_type is OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH:
            # Counterfactual Follow-Up Recurring Integration v1. Reuses
            # ``application.counterfactual_followup.SyncFollowUpObservations``
            # completely unmodified -- identical semantics, identical
            # identity/idempotency contract, identical due-state rules as
            # the manual ``followup-sync`` CLI path -- composed here only
            # with (1) the exact same repository/source objects the CLI
            # already wires (``self._followup.ledger``/``.subjects``/
            # ``.crypto_prices``, all zero-provider-request local reads) and
            # (2) one adapter-local Kalshi-request-budget wrapper around the
            # real, unmodified ``KalshiFollowUpPublicMarketSource``. The
            # budget is enforced entirely outside the application layer: once
            # exhausted, a further attempt raises the sync's own already-
            # handled ``FollowUpSourceUnavailable`` -- indistinguishable, from
            # ``SyncFollowUpObservations``'s point of view, from any other
            # genuine source outage, so no new failure path or retry logic
            # was added anywhere.
            #
            # Counterfactual Outcome Feedback v1, Phase 0: the Counterfactual
            # Decision Ledger itself (``domain.counterfactual_ledger``) was,
            # until this milestone, synchronized only by the manual
            # ``counterfactual-ledger-sync`` CLI command -- genuinely stale
            # by several operating days at audit time. Folded in here, ahead
            # of follow-up sync, rather than minted as its own new
            # ``OperatingTaskType`` (the "Autonomy Audit v1" precedent
            # immediately below this comment already established folding a
            # second, related service into this exact branch rather than
            # widening ``TASK_ORDER`` for a closely-coupled addition).
            # Composes the two existing, unmodified
            # ``SyncCounterfactualLedgerFromSharedOpportunity``/
            # ``...FromMandateEvaluations`` services (via
            # ``SyncCounterfactualLedger``) through
            # ``BatchedLedgerEvidenceSource`` -- a read-only, per-call
            # source that pre-batches every referenced
            # ``SharedOpportunityProjection`` lookup instead of the
            # CLI path's per-decision point reads, so running this every
            # five minutes stays bounded by the number of retained
            # opportunity *sets* rather than the number of retained
            # decisions (see that class's own docstring). Isolated in its
            # own try/except, identical in spirit to the evidence-quality
            # sweep below: a genuine ledger-sync failure must not prevent
            # follow-up sync/evidence-quality assessment from running
            # against whichever decisions are already retained -- an
            # un-synced new decision simply stays un-synced until a later
            # cycle succeeds, exactly like any other bounded, retried-by-
            # replay evidence gap in this task.
            try:
                # One instance implements both the shared-opportunity and
                # mandate evidence-source Protocols and pre-batches both
                # origins' projection lookups together on construction --
                # constructing it twice would repeat that batch read for no
                # reason, so the identical instance is passed for both.
                batched_ledger_evidence = BatchedLedgerEvidenceSource(self._followup.ledger)
                ledger_sync_result = SyncCounterfactualLedger(
                    self._followup.ledger,
                    batched_ledger_evidence,
                    batched_ledger_evidence,
                ).execute(evaluated_at=executed_at)
                ledger_sync_newly_written = ledger_sync_result.total_newly_written_count
                ledger_sync_warnings: tuple[str, ...] = (
                    f"LEDGER_ELIGIBILITY_CONSIDERED:"
                    f"{ledger_sync_result.eligibility_result.considered_count}",
                    f"LEDGER_ELIGIBILITY_NEWLY_WRITTEN:"
                    f"{ledger_sync_result.eligibility_result.newly_written_count}",
                    f"LEDGER_MANDATE_CONSIDERED:"
                    f"{ledger_sync_result.mandate_result.considered_count}",
                    f"LEDGER_MANDATE_NEWLY_WRITTEN:"
                    f"{ledger_sync_result.mandate_result.newly_written_count}",
                    f"LEDGER_TOTAL_NEWLY_WRITTEN:{ledger_sync_newly_written}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                ledger_sync_newly_written = 0
                ledger_sync_warnings = (f"LEDGER_SYNC_FAILED:{type(error).__name__}",)

            # Kalshi Decision-Linked Baseline Quote Evidence v1: captures
            # the decision-time quote for every retained KALSHI decision
            # (freshly synced above, or from any earlier cycle) that does
            # not yet have one -- the evidence
            # ``domain.counterfactual_outcome`` needs to ever reduce a
            # Kalshi cohort past ``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE``.
            # Placed immediately after ledger sync, ahead of classification
            # sync, since it operates directly on ledger decisions and has
            # no dependency on classification. Zero provider requests --
            # every field it persists is copied from already-retained,
            # already-immutable ``binary_complement_evaluation``/
            # ``threshold_bundle_evaluation`` evidence a decision already
            # references by exact id (see ``domain.kalshi_baseline_quote``).
            # Isolated in its own try/except for the identical reason as
            # ledger/classification sync above: a genuine failure here must
            # never turn this cycle's already-genuine follow-up sync into a
            # reported ``FAILED`` task; an uncaptured decision is retried by
            # this same bounded, self-healing sweep on a later cycle.
            try:
                baseline_quote_result = SyncKalshiBaselineQuoteEvidence(
                    self._kalshi_baseline_quote_source, self._kalshi_baseline_quote_source.quotes
                ).execute(
                    run_id=f"{cycle_id}-kalshi-baseline-quote",
                    generated_at=executed_at,
                    limit=DEFAULT_KALSHI_BASELINE_QUOTE_SWEEP_LIMIT,
                )
                baseline_quote_captured_count = baseline_quote_result.captured_count
                baseline_quote_warnings: tuple[str, ...] = (
                    f"KALSHI_BASELINE_QUOTE_CONSIDERED:{baseline_quote_result.considered_count}",
                    f"KALSHI_BASELINE_QUOTE_CAPTURED:{baseline_quote_result.captured_count}",
                    # Kalshi Baseline Quote Capture & Economic Maturation v1:
                    # breakdown of the total above by evidence kind --
                    # additive, the total itself is unchanged in meaning.
                    f"KALSHI_BASELINE_QUOTE_MATCHED_TRADE_CAPTURED:"
                    f"{baseline_quote_result.matched_trade_captured_count}",
                    f"KALSHI_BASELINE_QUOTE_MARKET_QUOTE_CAPTURED:"
                    f"{baseline_quote_result.market_quote_captured_count}",
                    f"KALSHI_BASELINE_QUOTE_REUSED_IDEMPOTENT:"
                    f"{baseline_quote_result.idempotent_replay_count}",
                    f"KALSHI_BASELINE_QUOTE_UNSUPPORTED_SOURCE_TYPE:"
                    f"{baseline_quote_result.unsupported_source_record_type_count}",
                    f"KALSHI_BASELINE_QUOTE_NO_CANDIDATE_EVALUATION:"
                    f"{baseline_quote_result.no_candidate_evaluation_count}",
                    # New: of the no-candidate decisions above, how many also
                    # could not produce a plain market-quote baseline
                    # (one-sided/missing/invalid orderbook) -- the honest,
                    # permanent "no usable baseline evidence of either kind"
                    # count.
                    f"KALSHI_BASELINE_QUOTE_INSUFFICIENT_MARKET_QUOTE_EVIDENCE:"
                    f"{baseline_quote_result.insufficient_market_quote_evidence_count}",
                    # Kalshi Baseline Temporal Semantics & Sweep Isolation
                    # v1: the total above broken down by reason family, so a
                    # future production regression is visible by cause
                    # (an operator reading these no longer has to guess
                    # whether 0 captures means "genuinely no eligible
                    # decisions" or "every attempt is quietly failing") --
                    # see ``application.kalshi_baseline_quote.
                    # SyncKalshiBaselineQuoteResult``'s own field docs.
                    f"KALSHI_BASELINE_QUOTE_MISSING_SNAPSHOT:"
                    f"{baseline_quote_result.missing_snapshot_count}",
                    f"KALSHI_BASELINE_QUOTE_ONE_SIDED_ORDERBOOK:"
                    f"{baseline_quote_result.one_sided_orderbook_count}",
                    f"KALSHI_BASELINE_QUOTE_MARKET_QUOTE_VALIDATION_REJECTED:"
                    f"{baseline_quote_result.market_quote_validation_rejected_count}",
                    f"KALSHI_BASELINE_QUOTE_MATCHED_TRADE_VALIDATION_REJECTED:"
                    f"{baseline_quote_result.matched_trade_validation_rejected_count}",
                    f"KALSHI_BASELINE_QUOTE_SOURCE_EVALUATION_NOT_FOUND:"
                    f"{baseline_quote_result.source_evaluation_not_found_count}",
                    "NO_PROVIDER_REQUEST_NO_PAPER_LIVE_OR_FINANCIAL_MUTATION",
                    *(
                        f"KALSHI_BASELINE_QUOTE_REJECTION_REASON:{reason.replace(' ', '_')}={count}"
                        for reason, count in baseline_quote_result.rejection_reason_counts
                    ),
                )
            except (OSError, RuntimeError, ValueError) as error:
                baseline_quote_captured_count = 0
                baseline_quote_warnings = (
                    f"KALSHI_BASELINE_QUOTE_SYNC_FAILED:{type(error).__name__}",
                )

            # Counterfactual Outcome Feedback v1, Phase 0 (continued):
            # ``domain.counterfactual_classification``'s own sync service was,
            # like ledger sync above, "deliberately CLI-triggered rather than
            # auto-wired" -- but Phase 1's outcome-aggregation policy requires
            # a decision's *current* classification (``REASON_CLASSIFIER_V2``)
            # to assign it to any reason-family cohort at all, so leaving this
            # CLI-only would mean every decision the sync above newly writes
            # is silently unclassifiable until a human separately remembers
            # to run ``counterfactual-classification-sync`` -- an ever-
            # growing coverage gap that gets *worse*, not better, the more
            # successfully ledger sync now keeps up. ``SyncCounterfactual
            # Classification`` already reads via one bulk ``all_decisions()``
            # call (confirmed by inspection: no per-decision fan-out to
            # optimize), so no batching adapter is needed here. Isolated in
            # its own try/except for the same reason as ledger sync above.
            try:
                classification_sync_result = SyncCounterfactualClassification(
                    self._followup.ledger, self._followup.ledger
                ).execute(classified_at=executed_at, classifier_version=CURRENT_CLASSIFIER_VERSION)
                classification_sync_newly_written = classification_sync_result.newly_written_count
                classification_sync_warnings: tuple[str, ...] = (
                    f"CLASSIFICATION_CONSIDERED:{classification_sync_result.considered_count}",
                    f"CLASSIFICATION_NEWLY_WRITTEN:{classification_sync_newly_written}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                classification_sync_newly_written = 0
                classification_sync_warnings = (
                    f"CLASSIFICATION_SYNC_FAILED:{type(error).__name__}",
                )

            kalshi_requests_this_cycle = [0]
            real_kalshi_source = self._followup_kalshi_source or KalshiFollowUpPublicMarketSource()

            class _BoundedKalshiFollowUpSource:
                """Implements ``application.counterfactual_followup.
                KalshiMarketSource``. Fails closed with zero further genuine
                requests once ``COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_
                PER_CYCLE`` is reached this cycle; every pair this blocks
                stays genuinely DUE and is picked up by a later cycle
                (deterministic oldest-due-first carry-forward, never
                dropped, never retried in a loop)."""

                def read_market_status(
                    self,
                    *,
                    subject_source_record_type: str,
                    instrument_id: str,
                    product_or_market_id: str,
                    timeout_seconds: int,
                ) -> KalshiMarketRead:
                    if (
                        kalshi_requests_this_cycle[0]
                        >= COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE
                    ):
                        raise FollowUpSourceUnavailable(
                            COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED,
                            request_count=0,
                        )
                    try:
                        read = real_kalshi_source.read_market_status(
                            subject_source_record_type=subject_source_record_type,
                            instrument_id=instrument_id,
                            product_or_market_id=product_or_market_id,
                            timeout_seconds=timeout_seconds,
                        )
                    except FollowUpSourceUnavailable as error:
                        kalshi_requests_this_cycle[0] += error.request_count
                        raise
                    kalshi_requests_this_cycle[0] += read.request_count
                    return read

            followup_sync_service = SyncFollowUpObservations(
                self._followup.ledger,
                self._followup.subjects,
                self._followup.crypto_prices,
                _BoundedKalshiFollowUpSource(),
                self._followup,
                # Securities Research -> Shared Opportunity Recurring Bridge
                # v1. Zero provider requests -- reads already-retained
                # options evidence only (see ``_SecuritiesFollowUpPriceSource``).
                securities_prices=self._followup.securities_prices,
                # Kalshi Settlement & Resolution Evidence Maturation v1: the
                # real, engine-backed, cross-cycle-durable repository -- see
                # ``self._kalshi_resolution_facts``'s own construction-site
                # comment for why this one dependency must never be left to
                # ``SyncFollowUpObservations``'s own in-memory default here.
                resolution_facts=self._kalshi_resolution_facts,
            )
            # Counterfactual Crypto Follow-Up Point-in-Time Semantics v2:
            # the recurring task always uses the explicit *current* policy
            # version -- never the domain builders' own backward-compatible
            # FOLLOWUP_POLICY_V1 default -- so unattended operation never
            # again produces a known-bad CRYPTO baseline.  Chosen once,
            # here, at the one composition point that matters for unattended
            # operation; nothing below this call re-derives or second-
            # guesses it.
            #
            # Counterfactual Forward Outcome Temporal Integrity v1: "the
            # current policy version" is now a per-venue *selection*
            # (CRYPTO -> FOLLOWUP_POLICY_V3, KALSHI and SECURITIES ->
            # FOLLOWUP_POLICY_V2), so this runs one bounded scan per
            # distinct version, each scoped to that version's own venues.
            # The service itself is constructed exactly once above, so the
            # shared per-cycle Kalshi request budget
            # (``kalshi_requests_this_cycle``) is genuinely shared across
            # both runs rather than being handed out twice.
            #
            # ``measurement_as_of`` is passed explicitly rather than left to
            # default to ``as_of``: the declaration is the point -- this is
            # the composition root that decides, once per cycle, the instant
            # every FOLLOWUP_POLICY_V3 endpoint lookup is bounded by, and a
            # reader of this file should not have to know a default to see
            # it happen.
            followup_policy_versions = CURRENT_FOLLOWUP_POLICY_VERSIONS
            # Counterfactual Follow-Up Daily Ceiling Lane Scope v1: today's
            # already-retained observation count, per lane, read *before*
            # this round runs -- because it is what decides whether each
            # lane runs at all.  Uses ``observations``'s own long-standing
            # ``policy_version`` filter (no new repository method), one
            # bounded read per lane, mirroring the one bounded scan per lane
            # immediately below.
            #
            # Read before rather than after so a lane that has genuinely
            # finished its day is *skipped* rather than run and then
            # retroactively judged.  Running it anyway would let a single
            # lane keep creating observations past its own ceiling for as
            # long as any other lane still had budget left -- which is
            # precisely the cross-lane coupling this milestone exists to
            # remove, re-introduced through the back door.
            followup_today_start = executed_at.replace(hour=0, minute=0, second=0, microsecond=0)
            followup_observations_today_before = {
                policy_version: len(
                    self._followup.observations(
                        start=followup_today_start, policy_version=policy_version
                    )
                )
                for policy_version in followup_policy_versions
            }
            followup_policy_versions_with_daily_budget = tuple(
                policy_version
                for policy_version in followup_policy_versions
                if followup_observations_today_before[policy_version]
                < COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
            )
            # Each run gets the full per-policy-version bound, so every
            # venue group keeps exactly the attempt throughput it already
            # had -- see ``COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_
            # VERSION``'s own comment for why splitting one bound across the
            # runs was evaluated and rejected.  Each version also keeps its
            # own durable scan cursor, so neither venue group's backlog can
            # starve the other's regardless of relative size.
            sync_results = tuple(
                followup_sync_service.execute(
                    as_of=executed_at,
                    sample_limit=COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION,
                    candidate_scan_limit=(
                        COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION
                    ),
                    policy_version=policy_version,
                    venues=current_followup_policy_venues(policy_version),
                    measurement_as_of=executed_at,
                )
                for policy_version in followup_policy_versions_with_daily_budget
            )
            # Aggregated across the selection's runs.  Deliberately plain
            # sums over genuinely disjoint runs (the selection partitions
            # venues, so no decision is counted twice) rather than a merged
            # ``FollowUpSyncResult``: that dataclass carries exactly one
            # ``policy_version``, and fabricating a merged one would be the
            # kind of "one field standing in for two different facts" this
            # module's own history keeps having to undo.
            followup_genuine_attempts = sum(item.genuine_attempts for item in sync_results)
            followup_created_count = sum(item.created_count for item in sync_results)
            followup_replay_count = sum(item.idempotent_replay_count for item in sync_results)
            followup_blocked_count = sum(item.blocked_count for item in sync_results)
            followup_permanently_unavailable_count = sum(
                item.permanently_unavailable_count for item in sync_results
            )
            followup_skipped_not_due_count = sum(
                item.skipped_not_due_count for item in sync_results
            )
            followup_skipped_unsupported_count = sum(
                item.skipped_unsupported_count for item in sync_results
            )
            followup_skipped_already_observed_count = sum(
                item.skipped_already_observed_count for item in sync_results
            )
            followup_provider_request_count = sum(
                item.provider_request_count for item in sync_results
            )
            followup_candidates_examined = sum(item.candidates_examined for item in sync_results)
            followup_sync_entries = tuple(entry for item in sync_results for entry in item.entries)

            # Counterfactual Follow-Up Backlog Throughput v1: this round
            # genuinely filled its own ``sample_limit`` -- a signal more
            # genuinely due work likely exists right now (Part: see
            # ``COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_
            # POLICY_VERSION``'s own module-level docstring) -- and that
            # same lane's independent daily ceiling has not yet been
            # reached. Both halves are per lane; see that constant for why
            # pairing a per-lane round bound with a subsystem-wide daily
            # bound silently halved every unchanged venue's throughput.
            # When both hold, ``application.operating_cycle.
            # ExecuteOperatingCycle`` defers saving today's terminal result
            # for this one task type and dispatches one more bounded round
            # on a later same-day tick instead (see ``TaskExecution.
            # awaits_intraday_continuation``'s own docstring) -- every other
            # outcome (round genuinely ran dry, or the daily ceiling is
            # reached) freezes today's result exactly as before this
            # milestone.
            # Counterfactual Follow-Up Daily Ceiling Lane Scope v1: each
            # lane's count after this round is its pre-round count plus
            # exactly what this round genuinely created for it.  Derived
            # rather than re-read: ``created_count`` is, by construction,
            # the number of rows this lane just committed, and this task is
            # the only writer of follow-up observations -- so the arithmetic
            # is exact, and it costs no second pass over today's rows.  A
            # lane that was skipped above simply keeps its pre-round count.
            followup_observations_today_by_policy_version = {
                **followup_observations_today_before,
                **{
                    item.policy_version: (
                        followup_observations_today_before[item.policy_version] + item.created_count
                    )
                    for item in sync_results
                },
            }
            followup_observations_today = sum(
                followup_observations_today_by_policy_version.values()
            )
            followup_awaits_intraday_continuation = (
                # Counterfactual Forward Outcome Temporal Integrity v1:
                # *any* run saturating its own bound is the signal that more
                # genuinely due work likely exists right now -- deliberately
                # not "every run saturated", which would silently stop
                # continuing whenever one venue group happened to run dry.
                #
                # Counterfactual Follow-Up Daily Ceiling Lane Scope v1: the
                # ceiling test is now inside that same per-lane predicate
                # rather than ANDed onto the whole disjunction.  A lane may
                # ask for another round only on its own evidence and its own
                # remaining budget, so one lane finishing its day can no
                # longer stop another lane's still-outstanding backlog, and
                # one lane's backlog can no longer buy another lane rounds
                # it has no budget left for.
                any(
                    item.genuine_attempts >= COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
                    and followup_observations_today_by_policy_version[item.policy_version]
                    < COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
                    for item in sync_results
                )
            )

            # Autonomy Audit v1: every retained follow-up observation must
            # eventually receive a deterministic evidence-quality assessment
            # without human intervention (Part: see ``COUNTERFACTUAL_
            # FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT_PER_CYCLE``'s own module-
            # level docstring for the full rationale).  Runs every cycle this
            # branch runs at all -- unconditionally, not only when ``sync_
            # result`` itself created something -- so a cycle where sync
            # finds nothing newly due still drains any outstanding
            # assessment backlog (new evidence created by a prior cycle that
            # crashed before assessing it; the pre-existing 92-row legacy
            # backlog, if the one-time CLI census has not been run yet).
            # Isolated in its own try/except, deliberately catching the
            # identical ``(OSError, RuntimeError, ValueError)`` triad the
            # outer per-task dispatch wrapper (``application.operating_
            # cycle._dispatch_task``) would otherwise catch: a genuine
            # failure here must never turn this cycle's already-committed,
            # already-genuine ``SyncFollowUpObservations`` success into a
            # reported task ``FAILED`` -- an assessment left unwritten this
            # cycle simply stays ``UNASSESSED`` (fail-closed, excluded from
            # downstream attribution by construction; see ``domain.
            # counterfactual_followup.is_evidence_eligible_for_downstream_
            # attribution``) and is retried by whichever future cycle next
            # reaches this branch, exactly like any other unassessed row.
            try:
                assessment_result = AssessFollowUpEvidenceQuality(
                    _BoundedUnassessedFollowUpEvidenceSource(
                        self._followup,
                        COUNTERFACTUAL_FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT_PER_CYCLE,
                    ),
                    self._followup,
                ).execute(
                    assessed_at=executed_at,
                    assessment_policy_version=CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
                )
                assessment_created_count = assessment_result.created_count
                assessment_warnings: tuple[str, ...] = (
                    f"FOLLOWUP_EVIDENCE_QUALITY_ASSESSED:{assessment_created_count}",
                    f"FOLLOWUP_EVIDENCE_QUALITY_ELIGIBLE:{assessment_result.eligible_count}",
                    f"FOLLOWUP_EVIDENCE_QUALITY_INELIGIBLE:{assessment_result.ineligible_count}",
                    f"FOLLOWUP_EVIDENCE_QUALITY_REUSED_IDEMPOTENT:"
                    f"{assessment_result.idempotent_replay_count}",
                    f"FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT:"
                    f"{COUNTERFACTUAL_FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT_PER_CYCLE}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                assessment_created_count = 0
                assessment_warnings = (
                    f"FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_SWEEP_FAILED:{type(error).__name__}",
                )

            # Securities Follow-Up Temporal Integrity v1: the SECURITIES
            # canonical verdict (``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2``) is
            # its own bounded lane with its own isolation, so a failure here
            # never disturbs the V1 sweep above (CRYPTO/KALSHI byte-for-byte
            # unchanged) or this cycle's already-committed sync, and an
            # unwritten V2 row simply stays fail-closed ``UNASSESSED`` for
            # SECURITIES consumers until a later cycle writes it.
            try:
                securities_v2_result = AssessFollowUpEvidenceQuality(
                    _BoundedUnassessedFollowUpEvidenceSource(
                        self._followup,
                        COUNTERFACTUAL_FOLLOWUP_EVIDENCE_QUALITY_SWEEP_LIMIT_PER_CYCLE,
                        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
                        venue=SharedVenue.SECURITIES,
                    ),
                    self._followup,
                ).execute(
                    assessed_at=executed_at,
                    assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
                )
                assessment_created_count += securities_v2_result.created_count
                assessment_warnings = (
                    *assessment_warnings,
                    f"FOLLOWUP_EVIDENCE_QUALITY_V2_ASSESSED:{securities_v2_result.created_count}",
                    f"FOLLOWUP_EVIDENCE_QUALITY_V2_ELIGIBLE:{securities_v2_result.eligible_count}",
                    f"FOLLOWUP_EVIDENCE_QUALITY_V2_INELIGIBLE:"
                    f"{securities_v2_result.ineligible_count}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                assessment_warnings = (
                    *assessment_warnings,
                    f"FOLLOWUP_EVIDENCE_QUALITY_V2_ASSESSMENT_SWEEP_FAILED:{type(error).__name__}",
                )

            created_ids = tuple(
                entry.observation_id
                for entry in followup_sync_entries
                if entry.outcome == "CREATED" and entry.observation_id is not None
            )
            reused_ids = tuple(
                entry.observation_id
                for entry in followup_sync_entries
                if entry.outcome == "REUSED_IDEMPOTENT_REPLAY" and entry.observation_id is not None
            )
            kalshi_budget_exhausted_this_cycle = any(
                entry.outcome == "BLOCKED_UNAVAILABLE"
                and entry.reasons == (COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED,)
                for entry in followup_sync_entries
            )
            changed = (
                followup_created_count > 0
                or assessment_created_count > 0
                or ledger_sync_newly_written > 0
                or classification_sync_newly_written > 0
                or baseline_quote_captured_count > 0
            )
            return TaskExecution(
                OperatingTaskStatus.COMPLETED if changed else OperatingTaskStatus.REUSED,
                tuple(sorted({*created_ids, *reused_ids})),
                reused_ids,
                changed,
                followup_provider_request_count,
                (
                    f"FOLLOWUP_CONSIDERED_PAIRS:{followup_genuine_attempts}",
                    f"FOLLOWUP_CREATED:{followup_created_count}",
                    f"FOLLOWUP_REUSED_IDEMPOTENT:{followup_replay_count}",
                    f"FOLLOWUP_BLOCKED_UNAVAILABLE:{followup_blocked_count}",
                    # Counterfactual Follow-Up Starvation Fix v1: a CRYPTO
                    # pair proven to be able to never produce genuine
                    # evidence (see ``FollowUpDueState.
                    # PERMANENTLY_UNAVAILABLE``) -- zero attempt/provider
                    # budget spent, deliberately its own warning code rather
                    # than folded into ``FOLLOWUP_BLOCKED_UNAVAILABLE`` above
                    # (Part: "expose them explicitly... rather than hiding
                    # semantics in one overloaded field").
                    f"FOLLOWUP_PERMANENTLY_UNAVAILABLE:{followup_permanently_unavailable_count}",
                    f"FOLLOWUP_SKIPPED_NOT_YET_DUE:{followup_skipped_not_due_count}",
                    f"FOLLOWUP_SKIPPED_UNSUPPORTED:{followup_skipped_unsupported_count}",
                    f"FOLLOWUP_SKIPPED_ALREADY_OBSERVED:{followup_skipped_already_observed_count}",
                    f"FOLLOWUP_PROVIDER_REQUESTS:{followup_provider_request_count}",
                    # Unchanged code and unchanged value -- it has always
                    # reported the bound one bounded scan runs under, which
                    # is now per policy version.  The honest per-cycle total
                    # is reported alongside it rather than silently folded
                    # into it.
                    f"FOLLOWUP_SAMPLE_LIMIT:"
                    f"{COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION}",
                    f"FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE:"
                    f"{COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE}",
                    # Counterfactual Follow-Up Starvation Fix v1: how many
                    # (decision, horizon_kind, baseline_kind) triples this
                    # cycle actually looked at (every outcome, not only
                    # genuine attempts) against the secondary wall-clock
                    # safety bound that examination is capped by -- an
                    # honest "did the scan itself stay bounded" signal,
                    # independent of the attempt/provider budgets above.
                    f"FOLLOWUP_CANDIDATES_EXAMINED:{followup_candidates_examined}",
                    f"FOLLOWUP_CANDIDATE_SCAN_LIMIT:"
                    f"{COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_POLICY_VERSION}",
                    f"FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_CYCLE:"
                    f"{COUNTERFACTUAL_FOLLOWUP_CANDIDATE_SCAN_LIMIT_PER_CYCLE}",
                    # Counterfactual Forward Outcome Temporal Integrity
                    # v1: one code per version in the current per-venue
                    # selection, in the same deterministic order the
                    # runs executed -- never one merged string, so an
                    # operator reading warnings sees exactly which
                    # versions genuinely ran this cycle.
                    *(f"FOLLOWUP_POLICY_VERSION:{item.policy_version}" for item in sync_results),
                    *(
                        (COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED,)
                        if kalshi_budget_exhausted_this_cycle
                        else ()
                    ),
                    *assessment_warnings,
                    *ledger_sync_warnings,
                    *classification_sync_warnings,
                    *baseline_quote_warnings,
                    # Counterfactual Follow-Up Backlog Throughput v1.
                    f"FOLLOWUP_OBSERVATIONS_TODAY:{followup_observations_today}",
                    f"FOLLOWUP_MAX_OBSERVATIONS_PER_DAY:"
                    f"{COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY}",
                    # Counterfactual Follow-Up Daily Ceiling Lane Scope v1:
                    # the two numbers immediately above are now *totals*
                    # over lanes, and a total alone cannot answer the
                    # question that actually matters when throughput looks
                    # wrong -- "which lane ran out, and was it the one I
                    # care about?".  One code per lane, every lane, in the
                    # same deterministic order the runs are declared in
                    # (including a lane that was skipped entirely this
                    # round, whose count is genuinely still true), plus an
                    # explicit code naming each lane that has genuinely
                    # finished its day.  Same "never one merged string"
                    # reasoning as ``FOLLOWUP_POLICY_VERSION`` above.
                    f"FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION:"
                    f"{COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION}",
                    *(
                        f"FOLLOWUP_OBSERVATIONS_TODAY:{policy_version}:"
                        f"{followup_observations_today_by_policy_version[policy_version]}"
                        for policy_version in followup_policy_versions
                    ),
                    *(
                        f"FOLLOWUP_DAILY_CEILING_REACHED:{policy_version}"
                        for policy_version in followup_policy_versions
                        if followup_observations_today_by_policy_version[policy_version]
                        >= COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION
                    ),
                    *(
                        ("FOLLOWUP_AWAITS_INTRADAY_CONTINUATION",)
                        if followup_awaits_intraday_continuation
                        else ()
                    ),
                    "NO_PAPER_LIVE_OR_FINANCIAL_MUTATION",
                ),
                awaits_intraday_continuation=followup_awaits_intraday_continuation,
            )

        if task.task_type is OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH:
            # Counterfactual Outcome Feedback v1, Phase 5. Reads currently-
            # retained decisions/classifications/follow-up observations/
            # evidence-quality assessments (kept current by the
            # ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` branch immediately above,
            # this cycle's own declared dependency), computes every
            # non-empty cohort's aggregate through the pure domain policy,
            # and persists each one idempotently. Zero provider requests;
            # ``run_id`` is cycle-scoped (mirrors ``StrategyHealthAssessment
            # .health_id``'s own ``f"{run_id}-..."`` idiom in ``adapters.
            # performance_attribution_postgres``/``application.performance_
            # attribution``) so a genuine crash-resume replay of the same
            # cycle reproduces byte-identical aggregate rows rather than
            # duplicating them, while each new day's cycle adds a fresh,
            # append-only rollup rather than mutating an earlier one.
            refresh_result = RefreshCounterfactualOutcomeAggregates(
                self._counterfactual_outcome_source, self._counterfactual_outcome
            ).execute(run_id=f"{cycle_id}-counterfactual-outcome", generated_at=executed_at)

            # Kalshi Decision-Linked Baseline Quote Evidence v1: an
            # additive second pass under ``COUNTERFACTUAL_OUTCOME_POLICY_
            # V2``, reusing the identical, unmodified service. A distinct
            # ``run_id`` (not merely a distinct ``outcome_policy_version``)
            # is deliberate: V2 is a byte-identical no-op for every CRYPTO
            # cohort (V2 does not change CRYPTO's own reduction at all --
            # see ``domain.counterfactual_outcome``'s module docstring), so
            # sharing V1's ``run_id`` would silently double the number of
            # aggregate rows ``aggregates_for_run(v1_run_id)`` returns for
            # every CRYPTO cohort each cycle -- a genuine storage/consumer-
            # confusion regression, not merely a cosmetic one. A separate
            # ``run_id`` keeps V1's own aggregate stream (and every existing
            # consumer's ``aggregates_for_run``/``earliest_qualified_per_
            # cohort`` reads of it) completely undisturbed; V2's own KALSHI-
            # capable aggregates live under their own, equally query-able,
            # cycle-scoped stream. Isolated in its own try/except: a genuine
            # V2 failure must never turn V1's already-succeeded, already-
            # persisted aggregation into a reported task ``FAILED``.
            try:
                refresh_result_v2 = RefreshCounterfactualOutcomeAggregates(
                    self._counterfactual_outcome_source, self._counterfactual_outcome
                ).execute(
                    run_id=f"{cycle_id}-counterfactual-outcome-v2",
                    generated_at=executed_at,
                    outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
                )
                v2_newly_written = refresh_result_v2.newly_written_count
                v2_aggregate_ids = refresh_result_v2.aggregate_ids
                v2_warnings: tuple[str, ...] = (
                    f"COUNTERFACTUAL_OUTCOME_V2_CONSIDERED:{refresh_result_v2.considered_count}",
                    f"COUNTERFACTUAL_OUTCOME_V2_NEWLY_WRITTEN:{v2_newly_written}",
                    f"COUNTERFACTUAL_OUTCOME_V2_REUSED_IDEMPOTENT:"
                    f"{refresh_result_v2.idempotent_replay_count}",
                    f"COUNTERFACTUAL_OUTCOME_V2_QUALIFIED:{refresh_result_v2.qualified_count}",
                    f"COUNTERFACTUAL_OUTCOME_V2_INSUFFICIENT_SAMPLE:"
                    f"{refresh_result_v2.insufficient_sample_count}",
                    f"COUNTERFACTUAL_OUTCOME_V2_VENUE_UNAVAILABLE:"
                    f"{refresh_result_v2.venue_unavailable_count}",
                    f"COUNTERFACTUAL_OUTCOME_V2_STALE_IDENTITY_SUPERSEDED:"
                    f"{refresh_result_v2.stale_identity_superseded_count}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                v2_newly_written = 0
                v2_aggregate_ids = ()
                v2_warnings = (f"COUNTERFACTUAL_OUTCOME_V2_REFRESH_FAILED:{type(error).__name__}",)

            # Counterfactual Outcome Evidence Specificity v3: a fourth pass,
            # CRYPTO-only under ``COUNTERFACTUAL_OUTCOME_POLICY_V3``, under
            # its own cycle-scoped ``run_id`` and its own try/except for the
            # identical reasons V2 has both (see the V2 comment above). Zero
            # provider requests; reads only the evidence V1 already read.
            try:
                refresh_result_v3 = RefreshCounterfactualOutcomeAggregates(
                    self._counterfactual_outcome_source, self._counterfactual_outcome
                ).execute(
                    run_id=f"{cycle_id}-counterfactual-outcome-v3",
                    generated_at=executed_at,
                    outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
                )
                v3_newly_written = refresh_result_v3.newly_written_count
                v3_aggregate_ids = refresh_result_v3.aggregate_ids
                v3_warnings: tuple[str, ...] = (
                    f"COUNTERFACTUAL_OUTCOME_V3_CONSIDERED:{refresh_result_v3.considered_count}",
                    f"COUNTERFACTUAL_OUTCOME_V3_NEWLY_WRITTEN:{v3_newly_written}",
                    f"COUNTERFACTUAL_OUTCOME_V3_REUSED_IDEMPOTENT:"
                    f"{refresh_result_v3.idempotent_replay_count}",
                    f"COUNTERFACTUAL_OUTCOME_V3_QUALIFIED:{refresh_result_v3.qualified_count}",
                    f"COUNTERFACTUAL_OUTCOME_V3_INSUFFICIENT_SAMPLE:"
                    f"{refresh_result_v3.insufficient_sample_count}",
                    f"COUNTERFACTUAL_OUTCOME_V3_STALE_IDENTITY_SUPERSEDED:"
                    f"{refresh_result_v3.stale_identity_superseded_count}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                v3_newly_written = 0
                v3_aggregate_ids = ()
                v3_warnings = (f"COUNTERFACTUAL_OUTCOME_V3_REFRESH_FAILED:{type(error).__name__}",)

            # Securities Structure Forward Performance v1: a third,
            # independent pass over the identical already-retained evidence
            # this task already reads, computing a *different* aggregate
            # (structure-type cohorts of ADVANCED SECURITIES decisions,
            # never the reason-family cohorts of rejected decisions V1/V2
            # compute -- see ``domain.securities_structure_outcome``'s own
            # module docstring for exactly why these are complementary, not
            # overlapping, mechanisms). Isolated in its own try/except for
            # the identical reason V2 is: a genuine failure here must never
            # turn V1/V2's already-succeeded, already-persisted aggregation
            # into a reported task ``FAILED``.
            try:
                securities_result = RefreshSecuritiesStructureOutcomeAggregates(
                    self._counterfactual_outcome_source, self._securities_structure_outcome
                ).execute(
                    run_id=f"{cycle_id}-securities-structure-outcome",
                    generated_at=executed_at,
                    # Securities Follow-Up Temporal Integrity v1: aggregate
                    # only over the canonical SECURITIES verdict (V2), so a
                    # terminal price from a later session never contributes.
                    # A V2 cohort id is distinct from its V1 predecessor's;
                    # retained V1 aggregates stay exactly as written.
                    evidence_quality_policy_version=(
                        CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION
                    ),
                )
                securities_newly_written = securities_result.newly_written_count
                securities_aggregate_ids = securities_result.aggregate_ids
                securities_warnings: tuple[str, ...] = (
                    f"SECURITIES_STRUCTURE_OUTCOME_CONSIDERED:{securities_result.considered_count}",
                    f"SECURITIES_STRUCTURE_OUTCOME_NEWLY_WRITTEN:{securities_newly_written}",
                    f"SECURITIES_STRUCTURE_OUTCOME_REUSED_IDEMPOTENT:"
                    f"{securities_result.idempotent_replay_count}",
                    f"SECURITIES_STRUCTURE_OUTCOME_QUALIFIED:{securities_result.qualified_count}",
                    f"SECURITIES_STRUCTURE_OUTCOME_INSUFFICIENT_SAMPLE:"
                    f"{securities_result.insufficient_sample_count}",
                    f"SECURITIES_STRUCTURE_OUTCOME_STALE_IDENTITY_SUPERSEDED:"
                    f"{securities_result.stale_identity_superseded_count}",
                )
            except (OSError, RuntimeError, ValueError) as error:
                securities_newly_written = 0
                securities_aggregate_ids = ()
                securities_warnings = (
                    f"SECURITIES_STRUCTURE_OUTCOME_REFRESH_FAILED:{type(error).__name__}",
                )

            changed = (
                refresh_result.newly_written_count > 0
                or v2_newly_written > 0
                or v3_newly_written > 0
                or securities_newly_written > 0
            )
            return TaskExecution(
                OperatingTaskStatus.COMPLETED if changed else OperatingTaskStatus.REUSED,
                (
                    *refresh_result.aggregate_ids,
                    *v2_aggregate_ids,
                    *v3_aggregate_ids,
                    *securities_aggregate_ids,
                ),
                (),
                changed,
                0,
                (
                    f"COUNTERFACTUAL_OUTCOME_CONSIDERED:{refresh_result.considered_count}",
                    f"COUNTERFACTUAL_OUTCOME_NEWLY_WRITTEN:{refresh_result.newly_written_count}",
                    f"COUNTERFACTUAL_OUTCOME_REUSED_IDEMPOTENT:"
                    f"{refresh_result.idempotent_replay_count}",
                    f"COUNTERFACTUAL_OUTCOME_QUALIFIED:{refresh_result.qualified_count}",
                    f"COUNTERFACTUAL_OUTCOME_INSUFFICIENT_SAMPLE:"
                    f"{refresh_result.insufficient_sample_count}",
                    f"COUNTERFACTUAL_OUTCOME_VENUE_UNAVAILABLE:"
                    f"{refresh_result.venue_unavailable_count}",
                    f"COUNTERFACTUAL_OUTCOME_STALE_IDENTITY_SUPERSEDED:"
                    f"{refresh_result.stale_identity_superseded_count}",
                    *v2_warnings,
                    *v3_warnings,
                    *securities_warnings,
                    "NO_PAPER_LIVE_OR_FINANCIAL_MUTATION",
                ),
            )

        raise OperatingCycleError("OPERATING_TASK_ADAPTER_UNAVAILABLE")

    def _ensure_crypto_paper_account(self, *, plan: OperatingCyclePlan) -> tuple[str, ...]:
        if self._crypto_paper.account(GENUINE_CRYPTO_PAPER_ACCOUNT_ID) is not None:
            return ()
        CreateCryptoPaperAccount(self._crypto_paper).execute(
            account_id=GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
            opening_cash=GENUINE_PAPER_OPENING_CASH,
            created_at=plan.created_at,
            idempotency_key=f"{GENUINE_CRYPTO_PAPER_ACCOUNT_ID}-bootstrap",
        )
        return ("CRYPTO_PAPER_ACCOUNT_BOOTSTRAPPED",)

    def _crypto_contract(
        self, product_id: str, *, universe: CryptoUniverseSnapshot | None, executed_at: datetime
    ) -> CryptoPaperProviderContract | None:
        contract_id = f"genuine-crypto-paper-contract-{product_id}"
        retained = self._crypto_paper.contract(contract_id)
        if retained is not None:
            return retained
        if universe is None:
            return None
        products = self._crypto.products(universe.source_capture_id)
        product = next((item for item in products if item.product_id == product_id), None)
        if product is None:
            return None
        built = build_provider_contract(
            contract_id=contract_id,
            product=product,
            source_capture_id=universe.source_capture_id,
        )
        # Recurring Strategy PAPER Orchestration v1: this exact idempotency
        # key -- "-key", not the previous "-register" -- must match
        # ExecuteStrategyPaperTrial.place_order's own internal
        # RegisterCryptoPaperProviderContract call
        # (f"{contract.contract_id}-key") byte-for-byte, since both call
        # sites can genuinely race to register the identical contract_id
        # for the same product (this method is shared by both the older
        # Cross-Venue-Allocation PAPER path and the new strategy-driven
        # one). _insert_immutable's own conflict rule is "same identity +
        # same canonical_hash + same idempotency_key -> safe replay;
        # anything else -> CRYPTO_PAPER_CONTRACT_IDENTITY_CONFLICT" -- a
        # mismatched suffix on an otherwise byte-identical contract was a
        # real, reachable false-positive conflict this fixes, not a stylistic
        # rename. Harmless for a contract_id already registered under the
        # old suffix: the retained-lookup above already short-circuits
        # before this line is ever reached again for that contract_id.
        RegisterCryptoPaperProviderContract(self._crypto_paper).execute(
            contract=built, idempotency_key=f"{contract_id}-key"
        )
        RetainCryptoPaperCapability(self._crypto_paper).execute(
            capability=inspect_paper_capability(contract=built, inspected_at=executed_at),
            idempotency_key=f"{built.contract_id}-capability",
        )
        return built

    def _crypto_price_evidence(
        self,
        product_id: str,
        *,
        pulse_id: str | None,
        cycle_id: str,
        executed_at: datetime,
    ) -> RetainedPaperPriceEvidence | None:
        if pulse_id is None:
            return None
        pulse = self._crypto.pulse_snapshot(pulse_id)
        if pulse is None:
            return None
        checkpoints = tuple(
            item
            for item in self._crypto.pulse_checkpoints(pulse.refresh_id)
            if item.product_id == product_id
            and item.best_bid is not None
            and item.best_ask is not None
            and item.midpoint is not None
        )
        if not checkpoints:
            return None

        def _observed(item: CryptoPulseCheckpoint) -> datetime:
            return item.quote_captured_at or item.status_observed_at

        latest = max(checkpoints, key=_observed)
        observed_at = _observed(latest)
        if executed_at - observed_at > CRYPTO_EVIDENCE_MAXIMUM_AGE:
            return None
        assert latest.midpoint is not None
        return RetainedPaperPriceEvidence.create(
            evidence_id=f"{cycle_id}-{product_id}-crypto-paper-price-evidence",
            product_id=product_id,
            observed_at=observed_at,
            ingested_at=executed_at,
            price=latest.midpoint,
            available_base_quantity=latest.visible_ask_depth,
            source=PaperPricingSource.GENUINE_TOP_OF_BOOK,
            evidence_class="GENUINE_PUBLIC",
        )

    def _execute_crypto_paper_trial(
        self,
        *,
        plan: OperatingCyclePlan,
        cycle_id: str,
        as_of: datetime,
        executed_at: datetime,
        state: RetainedOperatingState,
    ) -> TaskExecution:
        """Drive the existing internal Coinbase-compatible Crypto Spot PAPER engine
        from this cycle's CRYPTO PaperTrialProposals.  Every authorization, intent,
        preview, order, fill, expiry, and reconciliation uses a deterministic ID
        derived from retained evidence; nothing here contacts a Coinbase endpoint.

        CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: this method manages
        continuously open PAPER orders, not a rebuild-if-changed artifact -- there
        is no "done for today" terminal state, so the returned ``TaskExecution``
        always reports ``awaits_intraday_continuation=True`` (see that field's own
        docstring), exactly mirroring ``_execute_strategy_paper_trial``'s own use
        of this mechanism. Without it, ``application.operating_cycle.
        ExecuteOperatingCycle`` persists this dispatch's result as this UTC-day
        cycle_id's one-and-only terminal outcome and never calls this method again
        until the next day's cycle_id -- proven via a real-scheduler regression
        test (``tests/persistence/test_postgres_recurring_paper_trial.py``): an
        order created on the day's only real dispatch could never receive a later
        same-day fill or expiry, since both require evidence/wall-clock time
        strictly after ``order.submitted_at``/``order.valid_until``. A genuine
        exception still propagates to the shared ``_dispatch_task`` handler, which
        freezes this task ``FAILED`` for the rest of the UTC day -- the same
        fail-closed policy every other operating-cycle task already has,
        deliberately left unchanged.
        """

        if state.allocation_id is None:
            raise OperatingCycleError("OPERATING_CRYPTO_PAPER_ALLOCATION_MISSING")
        decision = self._cross_venue.decision(state.allocation_id)
        if decision is None:
            raise OperatingCycleError("OPERATING_CRYPTO_PAPER_ALLOCATION_MISSING")
        proposals = tuple(
            sorted(
                (
                    item
                    for item in self._cross_venue.proposals(decision.decision_id)
                    if item.venue is SharedVenue.CRYPTO
                ),
                key=lambda item: item.proposal_id,
            )
        )
        pulse_plan = self._crypto.refresh_plan(plan.crypto_pulse_plan_id)
        universe = (
            None
            if pulse_plan is None
            else self._crypto.universe_snapshot(pulse_plan.universe_snapshot_id)
        )
        warnings: list[str] = list(self._ensure_crypto_paper_account(plan=plan))
        outputs: list[str] = []
        replayed: list[str] = []
        rejected: list[str] = []
        eligible_count = 0
        orders_created = 0
        for proposal in proposals:
            projection = self._shared.projection(proposal.source_projection_id)
            shared_decision = (
                None
                if projection is None
                else next(
                    (
                        item
                        for item in self._shared.eligibility_decisions(decision.opportunity_set_id)
                        if item.projection_id == projection.projection_id
                    ),
                    None,
                )
            )
            if projection is None or shared_decision is None or universe is None:
                rejected.append(f"{proposal.proposal_id}:SOURCE_EVIDENCE_MISSING")
                continue
            contract = self._crypto_contract(
                projection.product_or_market_id, universe=universe, executed_at=executed_at
            )
            account = self._crypto_paper.account(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
            paper_state = self._crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
            account_balance: CryptoPaperAccountState | None = (
                None if paper_state is None else paper_state.account_state
            )
            eligibility = evaluate_paper_eligibility(
                decision_id=f"{cycle_id}-{proposal.proposal_id}-crypto-paper-eligibility",
                opportunity_set_id=decision.opportunity_set_id,
                projection=projection,
                shared_decision=shared_decision,
                universe=universe,
                contract=contract,
                account=account,
                account_state=account_balance,
                operating_cycle_enabled=plan.paper_eligible,
                evaluated_at=executed_at,
            )
            if eligibility.state.value != "ELIGIBLE":
                rejected.append(f"{proposal.proposal_id}:{'|'.join(eligibility.reasons)}")
                continue
            eligible_count += 1
            if account is None or paper_state is None or contract is None:
                rejected.append(f"{proposal.proposal_id}:ACCOUNT_OR_CONTRACT_MISSING")
                continue
            authorization_id = f"{proposal.proposal_id}-crypto-paper-authorization"
            existing = next(
                (
                    item
                    for item in paper_state.authorizations
                    if item.authorization_id == authorization_id
                ),
                None,
            )
            if existing is not None:
                replayed.append(authorization_id)
                continue
            maximum_notional = min(proposal.maximum_notional, MAX_ORDER_NOTIONAL)
            authorization = CryptoPaperAuthorization.create(
                authorization_id=authorization_id,
                account_id=account.account_id,
                eligibility=eligibility,
                projection=projection,
                maximum_quote_notional=maximum_notional,
                maximum_base_size=None,
                allowed_side=PaperSide.BUY,
                allowed_order_type=PaperOrderType.MARKET,
                expires_at=min(proposal.expires_at, executed_at + timedelta(hours=1)),
                actor=GENUINE_PAPER_ACTOR,
                reason=(
                    "Automated internal PAPER trial from cross-venue allocation "
                    f"proposal {proposal.proposal_id}."
                ),
                idempotency_key=f"{authorization_id}-key",
                created_at=executed_at,
            )
            AuthorizeCryptoPaperOpportunity(self._crypto_paper).execute(
                account_id=account.account_id, authorization=authorization
            )
            outputs.append(authorization_id)
            quote_size = _round_down(maximum_notional, contract.quote_increment)
            if quote_size < contract.minimum_quote_notional:
                warnings.append(f"{proposal.proposal_id}:MINIMUM_NOTIONAL_NOT_MET")
                continue
            evidence = self._crypto_price_evidence(
                projection.product_or_market_id,
                pulse_id=state.crypto_pulse_id,
                cycle_id=cycle_id,
                executed_at=executed_at,
            )
            if evidence is None:
                warnings.append(f"{proposal.proposal_id}:NO_FRESH_PRICE_EVIDENCE")
                continue
            intent_id = f"{authorization_id}-intent"
            intent = ConstructCryptoPaperOrderIntent(self._crypto_paper).execute(
                account_id=account.account_id,
                intent_id=intent_id,
                authorization_id=authorization_id,
                contract_id=contract.contract_id,
                side=PaperSide.BUY,
                order_type=PaperOrderType.MARKET,
                quote_size=quote_size,
                base_size=None,
                limit_price=None,
                created_at=executed_at,
                idempotency_key=f"{intent_id}-key",
            )
            outputs.append(intent.value.intent_id)
            preview_id = f"{authorization_id}-preview"
            preview = PreviewCryptoPaperOrder(self._crypto_paper).execute(
                account_id=account.account_id,
                preview_id=preview_id,
                intent_id=intent.value.intent_id,
                evidence=evidence,
                fee_bps=CONSERVATIVE_CRYPTO_FEE_BPS,
                spread_bps=CONSERVATIVE_CRYPTO_SPREAD_BPS,
                slippage_bps=CONSERVATIVE_CRYPTO_SLIPPAGE_BPS,
                created_at=executed_at,
                idempotency_key=f"{preview_id}-key",
            )
            outputs.append(preview.value.preview_id)
            if preview.value.rejection_reasons:
                rejected.append(
                    f"{proposal.proposal_id}:PREVIEW:{'|'.join(preview.value.rejection_reasons)}"
                )
                continue
            order_id = f"{authorization_id}-order"
            submission = SubmitCryptoPaperOrder(self._crypto_paper).execute(
                account_id=account.account_id,
                order_id=order_id,
                reservation_id=f"{order_id}-reservation",
                preview_id=preview.value.preview_id,
                submitted_at=executed_at,
                valid_until=executed_at + CRYPTO_ORDER_VALID_FOR,
                actor=GENUINE_PAPER_ACTOR,
                idempotency_key=f"{order_id}-key",
            )
            outputs.append(submission.value.order.order_id)
            if submission.idempotent_replay:
                replayed.append(submission.value.order.order_id)
            else:
                orders_created += 1

        account_state = self._crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
        fills_applied = 0
        expired_count = 0
        if account_state is not None:
            for order in account_state.orders:
                if order.state not in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}:
                    continue
                if executed_at >= order.valid_until:
                    # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: a
                    # raw splice here is safe from the fill_id/reconciliation_
                    # id *conflict* class of defect (expire is a one-time
                    # terminal transition per order, naturally guarded by the
                    # state filter above), but this milestone's own real-
                    # Postgres regression tests independently demonstrated it
                    # is NOT safe from the unbounded-splice *length* class of
                    # defect: a genuinely long proposal-derived order_id
                    # chained with cycle_id overflowed this exact field's
                    # VARCHAR(160) budget -- the identical defect ``apex-
                    # mandate-evaluation-varchar160-runtime-recovery-v1``
                    # already found and fixed elsewhere in this file, now
                    # also applied here via the same ``deterministic_id``
                    # convention.
                    ExpireCryptoPaperOrder(self._crypto_paper).execute(
                        account_id=GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
                        order_id=order.order_id,
                        expired_at=executed_at,
                        actor=GENUINE_PAPER_ACTOR,
                        idempotency_key=deterministic_id(
                            "crypto_paper_expire", (order.order_id, cycle_id)
                        ),
                    )
                    expired_count += 1
                    outputs.append(order.order_id)
                    continue
                fill_evidence = self._crypto_price_evidence(
                    order.product_id,
                    pulse_id=state.crypto_pulse_id,
                    cycle_id=cycle_id,
                    executed_at=executed_at,
                )
                if fill_evidence is None or fill_evidence.observed_at <= order.submitted_at:
                    # A fill must be justified by evidence observed strictly
                    # after the order was submitted; an order submitted this
                    # cycle is only fillable starting a later cycle.
                    continue
                # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: unlike
                # every other id in this loop, ``fill_id`` was scoped to
                # ``(order.order_id, cycle_id)`` alone -- harmless while an
                # order could receive at most one fill attempt per cycle_id,
                # but a liquidity-constrained order can genuinely partially
                # fill more than once within one UTC day now that this task
                # redispatches every tick (see ``awaits_intraday_
                # continuation`` above). A second same-cycle_id fill attempt
                # would reuse the FIRST attempt's identical fill_id/
                # idempotency_key, and ``ApplyCryptoPaperMarketEvidence``
                # would treat it as a replay of the first -- silently
                # discarding the second, genuinely different fill rather
                # than rejecting loudly (proven via a real-Postgres
                # RED->GREEN regression test on the identical KALSHI-side
                # defect; both venues share this exact application-layer
                # idempotency contract). A first attempt folding raw
                # ``executed_at`` into the id (making every genuine fill
                # attempt distinct while an exact replay of the identical
                # tick still matches) is exactly right in shape but genuinely
                # overflowed ``KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID``'s
                # ``VARCHAR(160)`` budget once chained onto an already-long
                # proposal/authorization/order id -- the identical
                # unbounded-splice class of defect
                # ``apex-mandate-evaluation-varchar160-runtime-recovery-v1``
                # already found and fixed elsewhere in this file.
                # ``deterministic_id`` is that same fix's bounded-derived-id
                # convention: collapsing arbitrary-length content into one
                # short, stable identifier -- same inputs always produce the
                # same key, so replay/idempotency semantics are unchanged.
                fill_id = deterministic_id(
                    "crypto_paper_fill", (order.order_id, cycle_id, executed_at.isoformat())
                )
                applied = ApplyCryptoPaperMarketEvidence(self._crypto_paper).execute(
                    account_id=GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
                    order_id=order.order_id,
                    fill_id=fill_id,
                    evidence=fill_evidence,
                    fee_bps=CONSERVATIVE_CRYPTO_FEE_BPS,
                    slippage_bps=CONSERVATIVE_CRYPTO_SLIPPAGE_BPS,
                    actor=GENUINE_PAPER_ACTOR,
                    idempotency_key=f"{fill_id}-key",
                )
                if applied.value is not None:
                    outputs.append(fill_id)
                    if not applied.idempotent_replay:
                        fills_applied += 1

        # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: scoped to
        # ``cycle_id`` alone -- harmless while this task genuinely dispatched
        # at most once per cycle_id, but once a cycle_id can span many
        # genuine dispatches (see ``awaits_intraday_continuation`` above), a
        # second same-cycle_id call reused the identical ``idempotency_key``
        # and ``ReconcileCryptoPaperAccount`` silently replayed the FIRST
        # dispatch's now-stale reconciliation record rather than genuinely
        # re-evaluating current account state -- reconciliation could never
        # progress intraday at all. ``deterministic_id`` folds ``executed_at``
        # in (making every genuine dispatch reconcile fresh) via the same
        # bounded-derived-id convention as ``fill_id`` above, rather than a
        # raw splice -- production's own real ``cycle_id`` is already ~66
        # chars (``f"{plan_id}-worker-{YYYYMMDD}"``), so this stays well
        # clear of the identical ``VARCHAR(160)`` class of defect fixed there.
        reconciliation_id = deterministic_id(
            "crypto_paper_reconciliation", (cycle_id, executed_at.isoformat())
        )
        reconciliation = ReconcileCryptoPaperAccount(self._crypto_paper).execute(
            account_id=GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
            reconciliation_id=reconciliation_id,
            evaluated_at=executed_at,
            order_id=None,
            idempotency_key=f"{reconciliation_id}-key",
        )
        outputs.append(reconciliation.value.reconciliation_id)
        mutated = bool(
            orders_created or fills_applied or expired_count or not reconciliation.idempotent_replay
        )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED if mutated else OperatingTaskStatus.REUSED,
            tuple(outputs),
            tuple(replayed),
            mutated,
            0,
            (
                f"CRYPTO_PAPER_PROPOSALS_CONSIDERED:{len(proposals)}",
                f"CRYPTO_PAPER_ELIGIBLE:{eligible_count}",
                f"CRYPTO_PAPER_ORDERS_CREATED:{orders_created}",
                f"CRYPTO_PAPER_FILLS_APPLIED:{fills_applied}",
                f"CRYPTO_PAPER_ORDERS_EXPIRED:{expired_count}",
                f"CRYPTO_PAPER_RECONCILIATION:{reconciliation.value.status.value}",
                *(f"CRYPTO_PAPER_REJECTED:{item}" for item in rejected),
                *warnings,
                "PAPER_ONLY",
                "NO_COINBASE_ORDER_SUBMISSION",
                "LIVE_AUTHORITY_NONE",
            ),
            # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: unconditional,
            # every dispatch -- see this method's own docstring and
            # TaskExecution.awaits_intraday_continuation's. A continuously
            # managed open PAPER position never "drains"; only a genuine
            # exception (handled generically by _dispatch_task, not here)
            # ever freezes this task for the rest of the UTC day.
            awaits_intraday_continuation=True,
        )

    def _ensure_kalshi_paper_account(self, *, plan: OperatingCyclePlan) -> tuple[str, ...]:
        if self._kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID) is not None:
            return ()
        CreateKalshiPaperAccount(self._kalshi).execute(
            account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID,
            opening_cash=GENUINE_PAPER_OPENING_CASH,
            created_at=plan.created_at,
            idempotency_key=f"{GENUINE_KALSHI_PAPER_ACCOUNT_ID}-bootstrap",
        )
        return ("KALSHI_PAPER_ACCOUNT_BOOTSTRAPPED",)

    def _kalshi_current_opportunity(
        self, *, projection: object, refresh_id: str
    ) -> KalshiCurrentOpportunity | None:
        source_record_id = getattr(projection, "source_record_id", None)
        market_ticker = getattr(projection, "product_or_market_id", None)
        return next(
            (
                item
                for item in self._kalshi.opportunities_for_refresh(refresh_id)
                if item.evaluation_id == source_record_id and item.market_ticker == market_ticker
            ),
            None,
        )

    def _execute_kalshi_paper_trial(
        self,
        *,
        plan: OperatingCyclePlan,
        cycle_id: str,
        as_of: datetime,
        executed_at: datetime,
        state: RetainedOperatingState,
    ) -> TaskExecution:
        """Drive the existing internal Kalshi PAPER engine from this cycle's KALSHI
        PaperTrialProposals, reusing this same cycle's already-retained current
        refresh.  Nothing here submits, cancels, or previews a genuine Kalshi order.

        CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: this method manages
        continuously open PAPER orders, not a rebuild-if-changed artifact -- there
        is no "done for today" terminal state, so the returned ``TaskExecution``
        always reports ``awaits_intraday_continuation=True`` (see that field's own
        docstring), exactly mirroring ``_execute_strategy_paper_trial``'s own use
        of this mechanism and ``_execute_crypto_paper_trial``'s identical fix
        immediately above. Without it, ``application.operating_cycle.
        ExecuteOperatingCycle`` persists this dispatch's result as this UTC-day
        cycle_id's one-and-only terminal outcome and never calls this method again
        until the next day's cycle_id -- proven via a real-scheduler regression
        test (``tests/persistence/test_postgres_recurring_paper_trial.py``): an
        order created on the day's only real dispatch could never receive a later
        same-day fill or expiry. Note KALSHI's real per-order validity window is
        the AUTHORIZATION's own ~1-hour expiry (``min(proposal.expires_at,
        executed_at + timedelta(hours=1))``, propagated preview -> order
        verbatim) -- ``KALSHI_ORDER_VALID_FOR`` below is dead code, never wired to
        an order's actual ``expires_at``; confirmed by grep and left unchanged, as
        removing genuinely-dead code is out of this milestone's narrow scope. A
        genuine exception still propagates to the shared ``_dispatch_task``
        handler, which freezes this task ``FAILED`` for the rest of the UTC day --
        the same fail-closed policy every other operating-cycle task already has,
        deliberately left unchanged.
        """

        if state.allocation_id is None:
            raise OperatingCycleError("OPERATING_KALSHI_PAPER_ALLOCATION_MISSING")
        decision = self._cross_venue.decision(state.allocation_id)
        if decision is None:
            raise OperatingCycleError("OPERATING_KALSHI_PAPER_ALLOCATION_MISSING")
        proposals = tuple(
            sorted(
                (
                    item
                    for item in self._cross_venue.proposals(decision.decision_id)
                    if item.venue is SharedVenue.KALSHI
                ),
                key=lambda item: item.proposal_id,
            )
        )
        refresh = self._kalshi.latest_refresh()
        warnings: list[str] = list(self._ensure_kalshi_paper_account(plan=plan))
        if refresh is None:
            warnings.append("KALSHI_CURRENT_REFRESH_UNAVAILABLE")
        outputs: list[str] = []
        replayed: list[str] = []
        rejected: list[str] = []
        eligible_count = 0
        orders_created = 0
        for proposal in proposals:
            projection = self._shared.projection(proposal.source_projection_id)
            shared_decision = (
                None
                if projection is None
                else next(
                    (
                        item
                        for item in self._shared.eligibility_decisions(decision.opportunity_set_id)
                        if item.projection_id == projection.projection_id
                    ),
                    None,
                )
            )
            opportunity = (
                None
                if projection is None or refresh is None
                else self._kalshi_current_opportunity(
                    projection=projection, refresh_id=refresh.refresh_id
                )
            )
            if (
                projection is None
                or shared_decision is None
                or opportunity is None
                or refresh is None
            ):
                rejected.append(f"{proposal.proposal_id}:SOURCE_EVIDENCE_MISSING")
                continue
            account = self._kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
            eligibility = KalshiPaperEligibility.evaluate(
                decision_id=f"{cycle_id}-{proposal.proposal_id}-kalshi-paper-eligibility",
                opportunity=opportunity,
                account=None if account is None else account.account,
                available_cash=(Decimal(0) if account is None else account.available_cash),
                shared_eligible=shared_decision.state.value == "ELIGIBLE",
                operating_cycle_enabled=plan.paper_eligible,
                evaluated_at=executed_at,
            )
            if not eligibility.eligible:
                rejected.append(f"{proposal.proposal_id}:{'|'.join(eligibility.reasons)}")
                continue
            eligible_count += 1
            if account is None:
                rejected.append(f"{proposal.proposal_id}:ACCOUNT_MISSING")
                continue
            authorization_id = f"{proposal.proposal_id}-kalshi-paper-authorization"
            existing = next(
                (
                    item
                    for item in account.authorizations
                    if item.authorization_id == authorization_id
                ),
                None,
            )
            if existing is not None:
                replayed.append(authorization_id)
                continue
            maximum_risk = min(proposal.maximum_notional, MAX_PAPER_ORDER_RISK)
            # Leave headroom below the risk cap for the preview's own fee
            # estimate (see `_fee` in domain/kalshi_operating.py) so a fully
            # authorized order is never rejected purely for exceeding its own
            # authorization once fees are added.
            maximum_contracts = (
                int((maximum_risk * Decimal("0.95")) / opportunity.price)
                if opportunity.price > 0
                else 0
            )
            if maximum_contracts < 1:
                rejected.append(f"{proposal.proposal_id}:MAXIMUM_CONTRACTS_BELOW_ONE")
                continue
            authorization = KalshiPaperAuthorization.create(
                authorization_id=authorization_id,
                account_id=account.account.account_id,
                opportunity=opportunity,
                eligibility=eligibility,
                maximum_contracts=maximum_contracts,
                maximum_dollar_risk=maximum_risk,
                expires_at=min(proposal.expires_at, executed_at + timedelta(hours=1)),
                actor=GENUINE_PAPER_ACTOR,
                reason=(
                    "Automated internal PAPER trial from cross-venue allocation "
                    f"proposal {proposal.proposal_id}."
                ),
                idempotency_key=f"{authorization_id}-key",
                created_at=executed_at,
            )
            AuthorizeKalshiPaperOpportunity(self._kalshi).execute(authorization=authorization)
            outputs.append(authorization_id)
            preview_id = f"{authorization_id}-preview"
            preview = PreviewKalshiPaperOrder(self._kalshi).execute(
                account_id=account.account.account_id,
                opportunity=opportunity,
                preview_id=preview_id,
                authorization_id=authorization_id,
                limit_price=opportunity.price,
                contracts=maximum_contracts,
                available_liquidity=opportunity.visible_liquidity,
                evidence_id=f"{refresh.refresh_id}-{opportunity.opportunity_id}-evidence",
                evidence_observed_at=opportunity.evidence_observed_at,
                created_at=executed_at,
                idempotency_key=f"{preview_id}-key",
            )
            outputs.append(preview.value.preview_id)
            if not preview.value.accepted:
                rejected.append(
                    f"{proposal.proposal_id}:PREVIEW:{'|'.join(preview.value.rejection_reasons)}"
                )
                continue
            order_id = f"{authorization_id}-order"
            submission = SubmitKalshiPaperOrder(self._kalshi).execute(
                account_id=account.account.account_id,
                order_id=order_id,
                preview_id=preview.value.preview_id,
                submitted_at=executed_at,
                idempotency_key=f"{order_id}-key",
            )
            outputs.append(submission.value.order_id)
            if submission.idempotent_replay:
                replayed.append(submission.value.order_id)
            else:
                orders_created += 1

        account = self._kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        fills_applied = 0
        expired_count = 0
        if account is not None and refresh is not None:
            fresh = {
                (item.market_ticker, item.side): item
                for item in self._kalshi.opportunities_for_refresh(refresh.refresh_id)
            }
            for order in account.orders:
                if order.state not in {
                    KalshiPaperOrderState.OPEN,
                    KalshiPaperOrderState.PARTIALLY_FILLED,
                }:
                    continue
                if executed_at >= order.expires_at:
                    # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: see
                    # the identical comment on ``_execute_crypto_paper_
                    # trial``'s own expire call above -- a real-Postgres
                    # regression test demonstrated this exact raw splice
                    # overflows ``VARCHAR(160)`` once chained onto a
                    # genuinely long proposal-derived order_id.
                    ExpireKalshiPaperOrder(self._kalshi).execute(
                        account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID,
                        order_id=order.order_id,
                        expired_at=executed_at,
                        idempotency_key=deterministic_id(
                            "kalshi_paper_expire", (order.order_id, cycle_id)
                        ),
                    )
                    expired_count += 1
                    outputs.append(order.order_id)
                    continue
                current = fresh.get((order.market_ticker, order.side))
                if (
                    current is None
                    or current.price > order.limit_price
                    # A fill must be justified by evidence observed strictly
                    # after the order was submitted (never the same evidence
                    # that priced the order into existence this cycle); an
                    # order submitted this cycle is only fillable starting a
                    # later cycle, once fresher evidence exists.
                    or current.evidence_observed_at <= order.submitted_at
                ):
                    continue
                remaining = order.contracts - order.filled_contracts
                fillable = min(remaining, max(0, current.visible_liquidity))
                if fillable < 1:
                    continue
                # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: unlike
                # every other id in this loop, ``fill_id`` was scoped to
                # ``(order.order_id, cycle_id)`` alone -- harmless while an
                # order could receive at most one fill attempt per cycle_id,
                # but a liquidity-constrained order (exactly what ``fillable``
                # above models) can genuinely partially fill more than once
                # within one UTC day now that this task redispatches every
                # tick (see ``awaits_intraday_continuation`` above). A second
                # same-cycle_id fill attempt would reuse the FIRST attempt's
                # identical fill_id/idempotency_key, and ``ApplyKalshiPaper
                # Fill`` would treat it as a replay of the first -- silently
                # discarding the second, genuinely different fill rather than
                # rejecting loudly (proven via a real-Postgres RED->GREEN
                # regression test: a 52-contract order partially filled 1
                # contract on one tick, then genuinely fillable for the
                # remaining 51 on a later same-cycle_id tick, stayed frozen
                # at 1 filled contract pre-fix). A first attempt folding raw
                # ``executed_at`` into the id (making every genuine fill
                # attempt distinct while an exact replay of the identical
                # tick still matches) is exactly right in shape but genuinely
                # overflowed ``apply_paper_fill``'s own
                # ``KALSHI_PAPER_IDEMPOTENCY_KEY_INVALID`` ``VARCHAR(160)``
                # budget once chained onto an already-long
                # proposal/authorization/order id -- caught by this
                # milestone's own real-Postgres regression test, the
                # identical unbounded-splice class of defect
                # ``apex-mandate-evaluation-varchar160-runtime-recovery-v1``
                # already found and fixed elsewhere in this file.
                # ``deterministic_id`` is that same fix's bounded-derived-id
                # convention: collapsing arbitrary-length content into one
                # short, stable identifier -- same inputs always produce the
                # same key, so replay/idempotency semantics are unchanged.
                fill_id = deterministic_id(
                    "kalshi_paper_fill", (order.order_id, cycle_id, executed_at.isoformat())
                )
                ApplyKalshiPaperFill(self._kalshi).execute(
                    account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID,
                    order_id=order.order_id,
                    fill_id=fill_id,
                    contracts=fillable,
                    price=current.price,
                    evidence_id=f"{refresh.refresh_id}-{current.opportunity_id}-evidence",
                    evidence_observed_at=current.evidence_observed_at,
                    ingested_at=executed_at,
                    idempotency_key=f"{fill_id}-key",
                )
                outputs.append(fill_id)
                fills_applied += 1

        # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: scoped to
        # ``cycle_id`` alone -- harmless while this task genuinely dispatched
        # at most once per cycle_id, but once a cycle_id can span many
        # genuine dispatches (see ``awaits_intraday_continuation`` above), a
        # second same-cycle_id call reused the identical ``idempotency_key``
        # and ``ReconcileKalshiPaperAccount`` silently replayed the FIRST
        # dispatch's now-stale reconciliation record rather than genuinely
        # re-evaluating current account state -- reconciliation could never
        # progress intraday at all. ``deterministic_id`` folds ``executed_at``
        # in (making every genuine dispatch reconcile fresh) via the same
        # bounded-derived-id convention as ``fill_id`` above, rather than a
        # raw splice -- production's own real ``cycle_id`` is already ~66
        # chars (``f"{plan_id}-worker-{YYYYMMDD}"``), so this stays well
        # clear of the identical ``VARCHAR(160)`` class of defect fixed there.
        reconciliation_id = deterministic_id(
            "kalshi_paper_reconciliation", (cycle_id, executed_at.isoformat())
        )
        reconciliation = ReconcileKalshiPaperAccount(self._kalshi).execute(
            account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID,
            reconciliation_id=reconciliation_id,
            reconciled_at=executed_at,
            idempotency_key=f"{reconciliation_id}-key",
        )
        outputs.append(reconciliation.value.reconciliation_id)
        mutated = bool(
            orders_created or fills_applied or expired_count or not reconciliation.idempotent_replay
        )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED if mutated else OperatingTaskStatus.REUSED,
            tuple(outputs),
            tuple(replayed),
            mutated,
            0,
            (
                f"KALSHI_PAPER_PROPOSALS_CONSIDERED:{len(proposals)}",
                f"KALSHI_PAPER_ELIGIBLE:{eligible_count}",
                f"KALSHI_PAPER_ORDERS_CREATED:{orders_created}",
                f"KALSHI_PAPER_FILLS_APPLIED:{fills_applied}",
                f"KALSHI_PAPER_ORDERS_EXPIRED:{expired_count}",
                f"KALSHI_PAPER_RECONCILIATION:{reconciliation.value.status.value}",
                *(f"KALSHI_PAPER_REJECTED:{item}" for item in rejected),
                *warnings,
                "PAPER_ONLY",
                "NO_KALSHI_ORDER_SUBMISSION",
                "KALSHI_SETTLEMENT_AWAITS_GENUINE_RESOLUTION_EVIDENCE",
                "LIVE_AUTHORITY_NONE",
            ),
            # CRYPTO/KALSHI PAPER Intraday Lifecycle Recovery v1: unconditional,
            # every dispatch -- see this method's own docstring and
            # TaskExecution.awaits_intraday_continuation's. A continuously
            # managed open PAPER position never "drains"; only a genuine
            # exception (handled generically by _dispatch_task, not here)
            # ever freezes this task for the rest of the UTC day.
            awaits_intraday_continuation=True,
        )

    def _ensure_strategy_crypto_paper_account(self, *, plan: OperatingCyclePlan) -> tuple[str, ...]:
        if self._crypto_paper.account(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID) is not None:
            return ()
        CreateCryptoPaperAccount(self._crypto_paper).execute(
            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
            opening_cash=GENUINE_PAPER_OPENING_CASH,
            created_at=plan.created_at,
            idempotency_key=f"{GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID}-bootstrap",
        )
        return ("STRATEGY_CRYPTO_PAPER_ACCOUNT_BOOTSTRAPPED",)

    def _strategy_paper_candle_history(
        self, *, plan: OperatingCyclePlan, ingested_at: datetime, as_of: datetime | None = None
    ) -> tuple[CryptoDailyCandle, ...]:
        """Genuine, retained BTC-USD daily closes -> the close-derived
        OHLC-shaped candles ``domain.strategy_benchmark.
        simulate_strategy_backtest`` expects. Reuses exactly the same
        retained CRYPTO_MARKET_PULSE/CRYPTO_UNIVERSE_REFRESH evidence
        ``_execute_crypto_paper_trial`` already depends on -- no new
        capture, no new provider request. See ``domain.strategy_paper_
        trial.daily_candles_from_close_observations``'s own docstring for
        why the close-only -> OHLC-shaped derivation is honest, not a
        fixture."""

        pulse_plan = self._crypto.refresh_plan(plan.crypto_pulse_plan_id)
        universe = (
            None
            if pulse_plan is None
            else self._crypto.universe_snapshot(pulse_plan.universe_snapshot_id)
        )
        if universe is None:
            return ()
        candidates = tuple(
            item
            for item in self._crypto.observations()
            if item.product_id == BENCHMARK_INSTRUMENT
            and item.source_capture_id == universe.source_capture_id
        )
        if not candidates:
            return ()
        best = max(candidates, key=lambda item: (len(item.candles), item.interval_end))
        candles = best.candles
        if as_of is not None:
            # Point-in-time cutoff (Historical Evidence Gap Recovery v1): a
            # cycle for ``as_of`` may only see candles of days completed before
            # that UTC day. Production passes today's midnight, which every
            # retained pulse candle already precedes, so this is a no-op there
            # and only constrains a deliberately backdated evaluation.
            cutoff = as_of.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            candles = tuple(item for item in candles if item.bucket_start < cutoff)
        return daily_candles_from_close_observations(
            candles,
            source_observation_id=best.observation_id,
            evidence_class=best.evidence_class,
            ingested_at=ingested_at,
        )

    def _strategy_paper_health_by_method(self) -> dict[str, StrategyHealthAssessment]:
        """The latest retained ``StrategyHealthAssessment`` per
        ``strategy_or_method_id`` -- closes the loop (Phase G) by feeding
        this recurring task's own prior-cycle attribution/health results
        back into ``domain.shared_opportunity.prioritize``'s existing
        ``SOURCE_HEALTH`` ranking component, exactly the mechanism that
        component was already built for."""

        latest: dict[str, StrategyHealthAssessment] = {}
        for item in sorted(
            self._performance.health_assessments(PaperEvidenceClass.GENUINE_PAPER),
            key=lambda value: (value.window_end, value.health_id),
        ):
            latest[item.strategy_or_method_id] = item
        return latest

    def _adopt_legacy_strategy_paper_trial_if_needed(
        self,
        *,
        definition: StrategyDefinition,
        strategy_version: int,
        instrument_id: str,
        existing_state: CryptoPaperState | None,
        open_orders_by_strategy: dict[str, CryptoPaperOrder],
    ) -> None:
        """Repeatable Strategy PAPER Evidence v1: if this slot has no trial
        record at all yet, but the OLD lifetime-only ``recurring_strategy_
        paper_trial_seed`` authorization already has a genuinely open
        position under it, adopt that position as this slot's generation-1
        trial -- see ``domain.strategy_paper_trial_lifecycle.
        adopt_legacy_trial`` and ``docs/REPEATABLE_STRATEGY_PAPER_EVIDENCE_
        V1.md``'s "Existing production position" section.

        ``strategy_version`` is passed explicitly (the caller's own "slot"
        version -- see ``_execute_strategy_paper_trial``'s own
        ``slot_strategy_version`` docstring note) rather than read off
        ``definition.version`` directly: this is called for every
        canonical candidate regardless of whether it promotes THIS cycle
        (closing/adopting a slot must never depend on today's fresh
        promotion outcome), so ``definition`` here is always the freshly
        reconstructed, un-promoted (``version=1``) catalog object -- using
        its own ``.version`` would reconstruct the WRONG (pre-promotion)
        legacy authorization id and never recognize the real, already-
        promoted-at-the-time lifetime authorization.

        Idempotent and self-healing: safe to call every cycle, for every
        candidate, unconditionally -- matches this whole task's
        established "every write here is already idempotent, so
        unconditional re-dispatch is always safe" policy (see
        ``_execute_strategy_paper_trial``'s own docstring). Once adoption
        has genuinely happened once, ``for_slot`` is non-empty on every
        later cycle and this becomes a single cheap no-op lookup."""

        if existing_state is None:
            return
        if self._strategy_paper_trials.for_slot(
            strategy_id=definition.strategy_id,
            strategy_version=strategy_version,
            instrument_id=instrument_id,
        ):
            return  # already has at least one trial record for this slot
        legacy_seed = recurring_strategy_paper_trial_seed(
            strategy_id=definition.strategy_id,
            strategy_version=strategy_version,
            instrument_id=instrument_id,
        )
        legacy_authorization_id = f"{legacy_seed}-authorization"
        open_order = open_orders_by_strategy.get(definition.strategy_id)
        if open_order is None or open_order.authorization_id != legacy_authorization_id:
            # Nothing genuinely open under the OLD lifetime seed for this
            # strategy -- either this slot has never traded at all (no
            # legacy position to adopt; a genuinely new generation-1 trial
            # will be opened through the normal retrial-eligible path
            # below), or the open position belongs to a DIFFERENT
            # authorization than the lifetime seed would predict (should
            # not occur for any candidate that has never had a trial
            # record, but this fails closed rather than guessing).
            return
        legacy_authorization = next(
            (
                item
                for item in existing_state.authorizations
                if item.authorization_id == legacy_authorization_id
            ),
            None,
        )
        if legacy_authorization is None:
            return
        position = next(
            (
                item
                for item in existing_state.positions
                if item.product_id == instrument_id and item.state == "OPEN"
            ),
            None,
        )
        baseline = Decimal(0) if position is None else position.realized_paper_pnl
        record = adopt_legacy_trial(
            strategy_id=definition.strategy_id,
            strategy_version=strategy_version,
            instrument_id=instrument_id,
            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
            authorization=legacy_authorization,
            order=open_order,
            opening_realized_pnl_baseline=baseline,
        )
        RecordStrategyPaperTrialOpened(self._strategy_paper_trials).execute(record=record)

    def _submit_strategy_paper_trial_close(
        self,
        *,
        definition: StrategyDefinition,
        evaluation: StrategyEvaluationResult,
        active_trial: StrategyPaperTrialRecord,
        existing_state: CryptoPaperState,
        universe: CryptoUniverseSnapshot | None,
        observation_result: ContemporaneousObservationResult,
        plan: OperatingCyclePlan,
        cycle_id: str,
        as_of: datetime,
        executed_at: datetime,
    ) -> tuple[str | None, str | None]:
        """Repeatable Strategy PAPER Evidence v1, Part 5D/C: if
        ``active_trial`` has reached its bounded evidence horizon and no
        closing SELL has been submitted for it yet, submit one through the
        SAME generic authorize -> construct -> preview -> submit sequence
        the opening BUY used (``ExecuteStrategyPaperTrial.close_position``)
        -- never a shortcut, never a fabricated fill. Returns ``(output_id,
        rejection_reason)`` -- at most one is non-``None``. Never writes a
        ``StrategyPaperTrialClosureRecord`` itself -- the SELL this submits
        still needs a LATER cycle's Phase F to actually fill it, exactly
        like the opening BUY did; finalizing the trial record happens in
        ``_finalize_strategy_paper_trial_closure``, called AFTER Phase F
        once this order's state is genuinely known."""

        if (
            evaluate_trial_horizon(trial=active_trial, as_of=as_of)
            is TrialHorizonState.BEFORE_HORIZON
        ):
            return None, None
        # PAPER Trial Closure Readiness v1: the close is a resumable state
        # machine classified from persisted economic state (see
        # ``classify_close_attempts``) -- never "an authorization exists, so
        # Phase F must be managing it".
        position = next(
            (
                item
                for item in existing_state.positions
                if item.product_id == active_trial.instrument_id and item.state == "OPEN"
            ),
            None,
        )
        attempts = classify_close_attempts(
            trial_id=active_trial.trial_id,
            authorizations=existing_state.authorizations,
            previews=existing_state.previews,
            orders=existing_state.orders,
            position_base_quantity=Decimal(0) if position is None else position.base_quantity,
            evaluated_at=executed_at,
        )
        if attempts.state in (CloseAttemptState.IN_FLIGHT, CloseAttemptState.FILLED):
            return None, None  # Phase F / Phase T' manage it
        if attempts.state is CloseAttemptState.DEAD_AWAITING_AUTHORIZATION_EXPIRY:
            return (
                None,
                f"{definition.strategy_id}:CLOSE_ATTEMPT_{attempts.attempt_number}_"
                f"{attempts.detail}_AWAITING_AUTHORIZATION_EXPIRY",
            )
        close_seed = attempts.action_seed
        assert close_seed is not None
        if position is None or position.base_quantity <= 0:
            return None, f"{definition.strategy_id}:TRIAL_HORIZON_REACHED_NO_OPEN_POSITION_TO_CLOSE"
        if observation_result.disposition is not ContemporaneousObservationDisposition.SATISFIED:
            return (
                None,
                f"{definition.strategy_id}:{observation_result.disposition.value}_"
                "CONTEMPORANEOUS_EVIDENCE_FOR_CLOSE",
            )
        contract = self._crypto_contract(
            active_trial.instrument_id, universe=universe, executed_at=executed_at
        )
        if universe is None or contract is None:
            return None, f"{definition.strategy_id}:CONTRACT_OR_UNIVERSE_UNAVAILABLE_FOR_CLOSE"
        assert observation_result.observation is not None
        decision_price_evidence = price_evidence_from_live_observation(
            observation_result.observation, product_id=active_trial.instrument_id
        )
        # PAPER Trial Closure Readiness v1: never mint close authority for a
        # quantity the contract itself can never accept (e.g. a partial
        # fill's residual below the minimum base size) -- construct/preview
        # would reject it after AUTHORIZE, and the retry policy would then
        # re-authorize a doomed attempt every hour. Fails closed, visibly.
        if (
            position.base_quantity < contract.minimum_base_size
            or position.base_quantity % contract.base_increment != 0
        ):
            return None, f"{definition.strategy_id}:CLOSE_QUANTITY_BELOW_CONTRACT_MINIMUM_BASE"
        if position.base_quantity * decision_price_evidence.price < contract.minimum_quote_notional:
            return (
                None,
                f"{definition.strategy_id}:CLOSE_QUANTITY_BELOW_CONTRACT_MINIMUM_QUOTE_NOTIONAL",
            )
        # The unmodified system-wide per-order cap would reject this close at
        # PREVIEW on every attempt -- disclosed limitation, surfaced here
        # before any authority is minted rather than as hourly dead attempts.
        if position.base_quantity * decision_price_evidence.price > MAX_ORDER_NOTIONAL:
            return None, f"{definition.strategy_id}:CLOSE_NOTIONAL_EXCEEDS_MAX_ORDER_NOTIONAL"
        projection = build_strategy_opportunity_projection(
            definition=definition,
            evaluation=evaluation,
            reference_price=decision_price_evidence.price,
            generated_at=executed_at,
            opportunity_ordinal=(
                f"{cycle_id}-{close_seed.removeprefix(f'{active_trial.trial_id}-')}"
            ),
        )
        try:
            placement = ExecuteStrategyPaperTrial(self._crypto_paper).close_position(
                definition=definition,
                projection=projection,
                contract=contract,
                universe=universe,
                account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                price_evidence=decision_price_evidence,
                base_quantity=position.base_quantity,
                operating_cycle_enabled=plan.paper_eligible,
                executed_at=executed_at,
                actor=STRATEGY_PAPER_TRIAL_ACTOR,
                idempotency_seed=close_seed,
            )
        except (StrategyPaperTrialError, CryptoPaperError) as error:
            return (
                None,
                f"{definition.strategy_id}:CLOSE_PLACEMENT_FAILED:{type(error).__name__}:{error}",
            )
        if placement.order is not None:
            return placement.order.order_id, None
        if placement.eligibility.state is not PaperEligibilityState.ELIGIBLE:
            return None, f"{definition.strategy_id}:CLOSE:{'|'.join(placement.eligibility.reasons)}"
        if placement.preview is not None and placement.preview.rejection_reasons:
            return (
                None,
                f"{definition.strategy_id}:CLOSE_PREVIEW:"
                f"{'|'.join(placement.preview.rejection_reasons)}",
            )
        if placement.preview is not None and executed_at >= placement.preview.expires_at:
            return None, f"{definition.strategy_id}:CLOSE_PREVIEW_EXPIRED_WITHOUT_ORDER"
        return None, f"{definition.strategy_id}:CLOSE_PLACEMENT_REJECTED"

    def _finalize_strategy_paper_trial_closure(
        self,
        *,
        active_trial: StrategyPaperTrialRecord,
        account_state: CryptoPaperState,
        executed_at: datetime,
    ) -> str | None:
        """Repeatable Strategy PAPER Evidence v1, Part 5D: called AFTER
        Phase F (this cycle's unconditional fill/expire pass), once
        ``account_state`` genuinely reflects any fill/expiry that just
        happened. Writes the trial's terminal ``StrategyPaperTrialClosureRecord``
        the moment either (a) the trial's OWN opening order is now
        confirmed dead without ever acquiring any exposure (``domain.
        strategy_paper_trial_lifecycle.build_unfilled_expiry_closure_record``
        -- Part 6's ``OPENING_ORDER_EXPIRED_UNFILLED`` boundary), or (b) a
        previously-submitted closing SELL is now confirmed ``FILLED``
        (``TRIAL_HORIZON_EXIT``, with this generation's own realized PAPER
        P&L computed as a delta against ``opening_realized_pnl_baseline`` --
        see that field's own docstring for why a delta, not the position's
        raw cumulative ``realized_paper_pnl``, is required). Returns a
        short warning-code string on any genuine state change, else
        ``None``. Idempotent: ``RecordStrategyPaperTrialClosed`` itself
        already guards a repeated call to this method against writing a
        second, conflicting closure."""

        opening_order = next(
            (
                item
                for item in account_state.orders
                if item.order_id == active_trial.opening_order_id
            ),
            None,
        )
        if opening_order is None:
            return None
        if opening_order.filled_base_quantity == 0 and opening_order.state in (
            PaperOrderState.EXPIRED,
            PaperOrderState.CANCELED,
        ):
            closure = build_unfilled_expiry_closure_record(
                trial=active_trial,
                closed_at=executed_at,
                coverage=self._trial_evidence_coverage(
                    active_trial=active_trial, closed_at=executed_at
                ),
            )
            RecordStrategyPaperTrialClosed(
                trials=self._strategy_paper_trials,
                closures=self._strategy_paper_trial_closures,
            ).execute(record=closure)
            return f"{active_trial.trial_id}:OPENING_ORDER_EXPIRED_UNFILLED"
        # PAPER Trial Closure Readiness v1: finalize on economic truth only --
        # the latest close attempt FILLED with the position genuinely flat
        # (``classify_close_attempts``), whichever attempt that was.
        position = next(
            (
                item
                for item in account_state.positions
                if item.product_id == active_trial.instrument_id
            ),
            None,
        )
        attempts = classify_close_attempts(
            trial_id=active_trial.trial_id,
            authorizations=account_state.authorizations,
            previews=account_state.previews,
            orders=account_state.orders,
            position_base_quantity=(
                Decimal(0)
                if position is None or position.state != "OPEN"
                else position.base_quantity
            ),
            evaluated_at=executed_at,
        )
        if attempts.state is not CloseAttemptState.FILLED:
            return None
        close_order = next(
            item for item in account_state.orders if item.order_id == attempts.order_id
        )
        realized = (
            None
            if position is None
            else position.realized_paper_pnl - active_trial.opening_realized_pnl_baseline
        )
        closure = build_strategy_paper_trial_closure_record(
            trial=active_trial,
            closed_at=executed_at,
            closing_authorization_id=close_order.authorization_id,
            closing_order_id=close_order.order_id,
            realized_paper_pnl=realized,
            coverage=self._trial_evidence_coverage(
                active_trial=active_trial, closed_at=executed_at
            ),
        )
        RecordStrategyPaperTrialClosed(
            trials=self._strategy_paper_trials, closures=self._strategy_paper_trial_closures
        ).execute(record=closure)
        return f"{active_trial.trial_id}:TRIAL_HORIZON_EXIT"

    def _trial_evidence_coverage(
        self, *, active_trial: StrategyPaperTrialRecord, closed_at: datetime
    ) -> TrialEvidenceCoverage:
        """Trial Evidence Coverage Disclosure v1: fetch every retained
        ``LiveMarketObservation`` for this trial's own instrument across its
        own real nominal window ``[opened_at, closed_at]`` and summarize
        retained-evidence density over it -- called exactly once, right
        before a closure record is actually about to be built (never
        unconditionally every cycle: the overwhelming majority of
        ``_finalize_strategy_paper_trial_closure`` calls return early with no
        closure to build at all, and this avoids a wasted repository query
        on every one of those). Purely descriptive -- see ``domain.
        strategy_paper_trial_evidence_coverage`` for the full contract; this
        method influences nothing about whether or how the trial closes."""

        observations = self._live_observation.observations_for_instrument_window(
            instrument_id=active_trial.instrument_id,
            window_start=active_trial.opened_at,
            window_end=closed_at,
        )
        return evaluate_trial_evidence_coverage(
            instrument_id=active_trial.instrument_id,
            window_start=active_trial.opened_at,
            window_end=closed_at,
            observations=observations,
        )

    def _execute_evidence_gap_recovery(
        self, *, cycle_id: str, as_of: datetime, executed_at: datetime
    ) -> TaskExecution:
        """Historical Evidence Gap Recovery v1: one bounded, deterministic
        recovery pass (see ``adapters.evidence_recovery_runner``).

        Records irrecoverable gaps (options chains, Kalshi refresh dates, live
        observation windows, missed PAPER execution windows) without any
        provider request, recovers missing XNYS-session securities bars through
        the existing historical capture pipeline, and reconciles/recovers
        crypto daily candles. Credentials absent is an honest warning, not a
        failure. Never creates a decision, cycle, live observation, order,
        fill or journal for a missed interval, and nothing in the PAPER
        lifecycle depends on it.
        """

        from apex.adapters.evidence_recovery_runner import (
            build_crypto_fetcher,
            build_securities_fetcher,
            run_evidence_gap_recovery,
        )

        report = run_evidence_gap_recovery(
            engine=self._engine,
            as_of=as_of,
            executed_at=executed_at,
            cycle_key=cycle_id,
            securities_fetcher=build_securities_fetcher(self._engine),
            crypto_fetcher=build_crypto_fetcher(self._crypto),
            crypto_repository=self._crypto,
        )
        outputs: list[str] = []
        warnings: list[str] = [
            f"EVIDENCE_GAP_IRRECOVERABLE_RECORDED:{family}:{count}"
            for family, count in sorted(report.irrecoverable_recorded.items())
        ]
        for label, summary in (("SECURITIES", report.securities), ("CRYPTO", report.crypto)):
            if summary is None:
                continue
            if summary.run is not None:
                outputs.append(summary.run.run_id)
            warnings.extend(
                (
                    f"EVIDENCE_RECOVERY_{label}_STATUS:{summary.status.value}",
                    f"EVIDENCE_RECOVERY_{label}_GAPS_DETECTED:{summary.gaps_detected}",
                    f"EVIDENCE_RECOVERY_{label}_GAPS_RECOVERED:{summary.gaps_recovered}",
                    f"EVIDENCE_RECOVERY_{label}_GAPS_IRRECOVERABLE:{summary.gaps_irrecoverable}",
                    f"EVIDENCE_RECOVERY_{label}_GAPS_CONFLICTED:{summary.gaps_conflicted}",
                    f"EVIDENCE_RECOVERY_{label}_VINTAGES_WRITTEN:{summary.vintages_written}",
                    *summary.warnings,
                )
            )
        warnings.extend(f"EVIDENCE_RECOVERY_ERROR:{item}" for item in report.errors)
        warnings.extend(("NO_PAPER_LIVE_OR_FINANCIAL_MUTATION", "LIVE_AUTHORITY_NONE"))
        # Only a failed *recovery* stage with nothing recovered is a task
        # failure. A live-only gap-detection problem is a warning, so it can
        # neither freeze the task for the UTC day nor raise a daily alert.
        recovery_errors = [item for item in report.errors if not item.startswith("LIVE_ONLY")]
        failed = bool(recovery_errors) and not outputs
        return TaskExecution(
            OperatingTaskStatus.FAILED if failed else OperatingTaskStatus.COMPLETED,
            tuple(outputs),
            (),
            bool(outputs),
            report.provider_requests,
            tuple(warnings),
            error_code=recovery_errors[0] if failed else None,
        )

    def _execute_strategy_trial_outcome_assessment(self, *, executed_at: datetime) -> TaskExecution:
        """Strategy PAPER Trial Outcome Assessment v1: append one immutable
        outcome assessment for every closed, not-yet-assessed strategy PAPER
        trial (including trials that closed before this task existed), in
        ``(closed_at, trial_id)`` order, at most
        ``STRATEGY_TRIAL_OUTCOME_MAX_ASSESSMENTS_PER_CYCLE`` per dispatch.

        Read-only toward the PAPER lifecycle: it never places, fills,
        expires, closes, reconciles, or attributes anything, makes zero
        provider requests, and nothing in the lifecycle depends on it. A
        per-trial lineage/economics problem is a warning on that trial only;
        a trial whose final attribution is not yet materialized is deferred.
        Keeps the task eligible for later same-day ticks while a backlog, a
        deferral, or an active trial (a close could land later today) exists.
        """

        result = AssessClosedStrategyPaperTrials(
            assessments=self._strategy_trial_outcomes,
            trials=self._strategy_paper_trials,
            closures=self._strategy_paper_trial_closures,
            provenance=self._strategy_provenance,
            evaluations=self._internal_strategy_evaluations,
            paper_states=self._crypto_paper,
            attributions=self._performance,
        ).execute(assessed_at=executed_at)
        active_trials = self._strategy_paper_trials.active_trials()
        return TaskExecution(
            OperatingTaskStatus.COMPLETED if result.written else OperatingTaskStatus.REUSED,
            result.written,
            result.replayed,
            bool(result.written),
            0,
            (
                f"STRATEGY_TRIAL_OUTCOME_ASSESSMENTS_WRITTEN:{len(result.written)}",
                f"STRATEGY_TRIAL_OUTCOME_ASSESSMENTS_REPLAYED:{len(result.replayed)}",
                f"STRATEGY_TRIAL_OUTCOME_BACKLOG_REMAINING:{result.backlog_remaining}",
                f"STRATEGY_TRIAL_OUTCOME_ACTIVE_TRIALS:{len(active_trials)}",
                *(f"STRATEGY_TRIAL_OUTCOME_DEFERRED:{item}" for item in result.deferred),
                *(f"STRATEGY_TRIAL_OUTCOME_FAILED:{item}" for item in result.failed),
                f"STRATEGY_TRIAL_OUTCOME_POLICY:{result.policy_version}",
                "NO_PROVIDER_REQUEST",
                "NO_PAPER_LIVE_OR_FINANCIAL_MUTATION",
                "LIVE_AUTHORITY_NONE",
            ),
            # A per-trial failure also keeps the task retrying on later
            # same-day ticks (Fable review #3), so it stays visible as
            # continuing work rather than a quiet REUSED terminal row.
            awaits_intraday_continuation=bool(
                result.backlog_remaining or result.deferred or result.failed or active_trials
            ),
        )

    def _execute_strategy_paper_trial(
        self,
        *,
        plan: OperatingCyclePlan,
        cycle_id: str,
        as_of: datetime,
        executed_at: datetime,
        state: RetainedOperatingState,
    ) -> TaskExecution:
        """Recurring Strategy PAPER Orchestration v1: drive
        ``ExecuteStrategyPaperTrial`` from the operating cycle.

        Structurally independent of Cross-Venue-Allocation/
        SHARED_OPPORTUNITY_REBUILD: candidates are the canonical EXTERNAL
        benchmark strategies (``domain.strategy_benchmark.
        canonical_strategy_benchmarks``), evaluated fresh each cycle against
        genuinely retained BTC-USD close history, never sourced from that
        other pipeline. ``crypto-paper-capability-inspect``'s capability-
        observation gate (``domain.cross_venue_allocation.
        allocate_cross_venue``'s ``VENUE_PAPER_BOUNDARY_UNAVAILABLE``) is
        scoped entirely to that OLDER path -- ``evaluate_paper_eligibility``/
        ``ExecuteStrategyPaperTrial`` read no capability-observation table at
        all (Phase A archaeology) -- so this task is neither blocked by, nor
        needs, that inspection. A dedicated PAPER account
        (``GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID``) keeps this path's own
        ``MAX_OPEN_ORDERS_PER_PRODUCT`` bookkeeping from ever colliding with
        the older path's.

        Each cycle: (1) evaluate every candidate against genuinely retained
        BTC-USD close history, durably retain that exact evaluation result
        (Strategy Evaluation History & Promotion Lineage v1 -- see
        ``domain.strategy_evaluation_history``; deterministic identity
        means a later same-day tick replays the identical row rather than
        duplicating it), and evidence-gate promotion
        (RESEARCH_ONLY -> PAPER_ELIGIBLE, Phase B -- this per-cycle gate
        check itself remains a purely in-memory decision, exactly as
        before this milestone: it decides whether a candidate may trade
        THIS cycle, and never durably mutates any strategy's lifecycle
        state -- see that module's own docstring for why "would qualify"
        and "is currently being traded" can both be true while the durable
        operational lifecycle state honestly stays RESEARCH_ONLY absent an
        operator's own explicit, separate promotion action) -- Strategy
        Version Provenance v1: the exact evaluated artifact, its evaluation
        binding, and the admission transition to the traded v2 artifact are
        retained before any placement, and a new trial opens atomically with
        its provenance (see ``domain.strategy_version_provenance``); (2)
        obtain ONE canonical
        contemporaneous market observation (Phase C) shared by every
        promoted candidate (they all target the same instrument), failing
        closed on STALE/ABSENT rather than falling back to a fresh direct
        Coinbase capture; (3) classify each promoted candidate's OWN
        bounded trial slot -- adopt any pre-migration legacy position as a
        genuine generation-1 trial, recognize an already-ACTIVE trial (and,
        if its evidence horizon is reached, submit its closing SELL through
        the SAME generic authorize/construct/preview/submit sequence the
        opening BUY used), or determine retrial eligibility (cooldown +
        still qualifies) for a slot with no active trial (Phase T -- see
        ``domain.strategy_paper_trial_lifecycle``, Repeatable Strategy PAPER
        Evidence v1); (4) rank candidates genuinely eligible for a fresh
        trial generation with the existing evidence-weighted
        ``prioritize()`` (fed this task's own retained strategy-health
        evidence) and bound-select at most
        ``MAX_NEW_STRATEGY_PAPER_TRIALS_PER_CYCLE`` (Phase E); (5) place a
        new deterministic trial generation for the selected candidate, and
        durably retain its trial-open record -- ``strategy_paper_trial_id``
        folds in ``generation`` (cycle-independent, so a repeated replay or
        a later cycle both recognize "already submitted this generation"
        without any new registration); (6) unconditionally continue every
        currently-open order's lifecycle (fill/expire), mirroring
        ``_execute_crypto_paper_trial`` exactly, THEN finalize any trial a
        just-applied fill or expiry genuinely resolved (Phase T', after
        Phase F); (7) reconcile; (8) automatically materialize attribution
        + strategy health for every tracked order (Phase G), open or
        closed, threading this cycle's own retained Total Market Context id
        into attribution lineage where one exists (Phase D).

        Repeatable Strategy PAPER Evidence v1 note: this method's per-cycle
        trading decision is still the same fresh in-memory gate check as
        before (nothing here durably promotes a strategy) -- what changed
        is that the OLD lifetime-only ``recurring_strategy_paper_trial_
        seed`` no longer gates placement directly. It remains in use only
        for recognizing the pre-migration legacy authorization during
        adoption; every trial this method places going forward is
        identified by ``strategy_paper_trial_id`` (which additionally folds
        in ``generation``), and a slot's trial history/cooldown/horizon are
        read from the durable ``internal_strategy_paper_trial``/
        ``internal_strategy_paper_trial_closure`` tables, never inferred
        from ``crypto_paper`` state alone.

        Recurring Strategy PAPER Intraday Execution Correction v1: this
        method manages a continuously open position, not a rebuild-if-
        changed artifact -- there is no "done for today" terminal state the
        way a bounded backlog can drain or a calendar window can close, so
        the returned ``TaskExecution`` always reports ``awaits_intraday_
        continuation=True`` (see that field's own docstring). Without it,
        ``application.operating_cycle.ExecuteOperatingCycle`` would persist
        this dispatch's result as this UTC-day cycle_id's one-and-only
        terminal outcome and never call this method again until the next
        day's cycle_id -- steps (1)-(7) above would genuinely run exactly
        once per day rather than on every operating-cycle tick, so a later
        same-day tick could never observe fresher ``LiveMarketObservation``
        evidence, fill or expire an already-open order, or attribute a
        same-day close. Every mutation this method performs is
        independently idempotent via its own deterministic ids regardless
        of how often it is called (see the module-level docstring above),
        so unconditional re-dispatch is always safe; a genuine exception
        still propagates to the shared ``_dispatch_task`` handler, which
        records a normal ``FAILED`` terminal result and freezes this task
        for the rest of the UTC day -- the same fail-closed policy every
        other operating-cycle task already has, deliberately left
        unchanged.
        """

        warnings: list[str] = list(self._ensure_strategy_crypto_paper_account(plan=plan))
        rejected: list[str] = []
        outputs: list[str] = []
        replayed: list[str] = []
        trials_created = 0
        # Strategy Evaluation History & Promotion Lineage v1.
        retained_evaluation_ids: list[str] = []
        replayed_evaluation_ids: list[str] = []
        # Strategy Version Provenance v1: the admission transition each
        # promoted candidate is allowed to open a trial under this cycle.
        admission_transition_ids: dict[str, str] = {}
        admissions_retained = 0

        candidates = canonical_strategy_benchmarks(created_at=executed_at)
        candle_history = self._strategy_paper_candle_history(
            plan=plan, ingested_at=executed_at, as_of=as_of
        )
        # Every candle in one _strategy_paper_candle_history() batch shares
        # the identical source observation id (see that method's own
        # "best = max(...)" single-batch selection) -- a real, meaningful
        # evidence-window identifier, reused here exactly as ``evidence_
        # provenance`` immediately below already reuses it. Never read when
        # candle_history is empty: every candidate's own INSUFFICIENT_
        # RETAINED_CANDLE_HISTORY rejection fires first in that case (0 <
        # any positive minimum_history_days), so this is unreachable then.
        capture_id = candle_history[0].capture_id if candle_history else ""

        promoted: list[tuple[StrategyDefinition, StrategyEvaluationResult]] = []
        # Repeatable Strategy PAPER Evidence v1: EVERY candidate that gets a
        # real evaluation this cycle, regardless of whether it goes on to
        # promote -- keyed by strategy_id, holding the fresh (un-promoted,
        # always version=1) definition. Closing an already-ACTIVE trial
        # (Phase T below) must never depend on the strategy re-qualifying
        # THIS cycle -- a strategy whose fresh evaluation now looks
        # unfavorable is exactly a candidate whose open trial we still want
        # to observe through to its own honest horizon, not one that gets
        # silently skipped and left stuck open. ``promoted`` remains the
        # separate, narrower set that gates OPENING a new trial generation,
        # unchanged in meaning.
        evaluated_by_strategy: dict[str, tuple[StrategyDefinition, StrategyEvaluationResult]] = {}
        for definition in candidates:
            if len(candle_history) < definition.evidence_requirements.minimum_history_days:
                rejected.append(f"{definition.strategy_id}:INSUFFICIENT_RETAINED_CANDLE_HISTORY")
                continue
            try:
                backtest = simulate_strategy_backtest(
                    definition,
                    candle_history,
                    evidence_class=candle_history[0].evidence_class,
                    evidence_provenance=candle_history[0].capture_id,
                )
            except StrategyBenchmarkError as error:
                rejected.append(f"{definition.strategy_id}:BACKTEST_FAILED:{type(error).__name__}")
                continue
            evaluation = evaluate_external_strategy(definition, backtest)
            evaluated_by_strategy[definition.strategy_id] = (definition, evaluation)
            # Strategy Evaluation History & Promotion Lineage v1: durably
            # retain the EXACT evaluation result this dispatch just
            # computed, before the pre-existing (unmodified) promotion-gate
            # check immediately below decides whether it may trade this
            # cycle -- so even a candidate that stays RESEARCH_ONLY this
            # cycle gets an honest, immutable evaluation row (Part 8 case
            # 1). Deterministic identity means every later same-day tick
            # (same as_of, same evidence) replays this exact row rather
            # than duplicating it (Part 6); a genuine failure here
            # propagates like any other write in this method, freezing the
            # task FAILED for the day via the shared _dispatch_task
            # handler -- deliberately no special-case try/except, matching
            # this method's own established "every write here is already
            # idempotent, so unconditional re-dispatch is always safe"
            # policy (see this method's own docstring).
            recorded = RecordInternalStrategyEvaluation(
                self._internal_strategy_evaluations
            ).execute(
                definition=definition,
                candidate=evaluation,
                cycle_id=cycle_id,
                as_of=as_of,
                capture_id=capture_id,
                evaluated_at=executed_at,
            )
            if recorded.idempotent_replay:
                replayed_evaluation_ids.append(recorded.record.evaluation_id)
            else:
                retained_evaluation_ids.append(recorded.record.evaluation_id)
            # Strategy Version Provenance v1: bind this evaluation to the
            # exact artifact it evaluated (the in-memory v1 definition just
            # used), before the gate check below. A provenance failure never
            # raises out of this task: it blocks OPENING a new trial for this
            # candidate only (see below). Closing, fills, expiry, and
            # reconciliation continue unaffected.
            provenance_error: str | None = None
            try:
                RecordEvaluationSubject(self._strategy_provenance).execute(
                    definition=definition,
                    registry=StrategyVersionRegistry.CATALOG_CODE,
                    evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
                    evaluation_id=recorded.record.evaluation_id,
                    evaluation_hash=recorded.record.canonical_hash,
                    evaluated_strategy_id=recorded.record.strategy_id,
                    evaluated_strategy_version=recorded.record.strategy_version,
                )
            except (
                StrategyVersionProvenanceError,
                StrategyVersionProvenanceResearchError,
            ) as error:
                provenance_error = str(error)
            try:
                promoted_definition = promote_to_paper_eligible(
                    definition, evaluation=evaluation, promoted_at=executed_at
                )
            except StrategyEvaluationError as error:
                rejected.append(f"{definition.strategy_id}:PROMOTION_REJECTED:{error}")
                continue
            # The in-memory gate admission (v1 -> v2) that actually permits
            # trading this cycle is retained as an immutable transition bound
            # to this exact evaluation. Provenance is written BEFORE any
            # placement: PAPER state writes commit inside place_order, so the
            # binding must already exist by the time a trial can open.
            if provenance_error is None:
                try:
                    admission = RecordStrategyVersionTransition(self._strategy_provenance).execute(
                        kind=StrategyVersionTransitionKind.CYCLE_GATE_ADMISSION,
                        from_definition=definition,
                        to_definition=promoted_definition,
                        evaluation_source=StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION,
                        evaluation_id=recorded.record.evaluation_id,
                        evaluation=evaluation,
                        registry=StrategyVersionRegistry.CATALOG_CODE,
                    )
                except (
                    StrategyVersionProvenanceError,
                    StrategyVersionProvenanceResearchError,
                ) as error:
                    provenance_error = str(error)
                else:
                    admission_transition_ids[definition.strategy_id] = (
                        admission.transition.transition_id
                    )
                    if not admission.idempotent_replay:
                        admissions_retained += 1
            if provenance_error is not None:
                rejected.append(
                    f"{definition.strategy_id}:STRATEGY_PAPER_TRIAL_OPEN_BLOCKED_PROVENANCE:"
                    f"{provenance_error}"
                )
                continue
            promoted.append((promoted_definition, evaluation))
        promoted_by_strategy_id = {item[0].strategy_id: item for item in promoted}

        # ---- Phase C: ONE canonical contemporaneous observation, shared by
        # every promoted candidate this cycle (every canonical benchmark
        # targets the identical instrument). Fails closed on STALE/ABSENT --
        # fallback_source is never supplied, so there is no automatic direct
        # Coinbase escape hatch here.
        max_staleness = (
            candidates[0].evidence_requirements.maximum_observation_staleness_seconds or 1800
        )
        observation_result = ObtainContemporaneousMarketObservation(self._live_observation).execute(
            venue=SharedVenue.CRYPTO,
            instrument_id=BENCHMARK_INSTRUMENT,
            observation_type=LiveObservationType.QUOTE,
            as_of=executed_at,
            max_staleness_seconds=max_staleness,
        )

        # ---- Phase T (Repeatable Strategy PAPER Evidence v1): classify
        # each promoted candidate's OWN trial slot. Checked FIRST,
        # independent of ranking -- an existing trial's continued-open
        # status must never depend on fresh decision-time evidence being
        # available today (only placing/closing a trial needs that).
        # Deterministic and cycle-independent (``strategy_paper_trial_id``/
        # ``next_trial_generation`` are pure functions of already-retained
        # rows), so this recognizes "already active"/"eligible for retrial"
        # correctly even on a cycle where evidence is STALE/ABSENT.
        existing_state = self._crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        open_orders_by_strategy = (
            open_trial_orders_by_strategy(
                positions=existing_state.positions,
                orders=existing_state.orders,
                authorizations=existing_state.authorizations,
            )
            if existing_state is not None
            else {}
        )
        pulse_plan = self._crypto.refresh_plan(plan.crypto_pulse_plan_id)
        universe = (
            None
            if pulse_plan is None
            else self._crypto.universe_snapshot(pulse_plan.universe_snapshot_id)
        )

        active_instrument_ids: set[str] = set()
        slot_trials_by_strategy: dict[str, tuple[StrategyPaperTrialRecord, ...]] = {}
        retrial_eligible: list[tuple[StrategyDefinition, StrategyEvaluationResult]] = []
        for definition in candidates:
            instrument_id = definition.instrument_ids[0]
            # Repeatable Strategy PAPER Evidence v1: iterates the FULL
            # canonical catalog, not just `promoted` -- recognizing an
            # already-ACTIVE trial and closing it once its horizon is
            # reached must never depend on this strategy re-qualifying
            # THIS cycle (a strategy whose evaluation now looks unfavorable
            # is exactly the case where we still want its open trial to
            # reach its own honest, already-committed horizon, never one
            # that gets silently skipped and left stuck open). Opening a
            # NEW trial generation still strictly requires fresh
            # qualification -- enforced below via `promoted_by_strategy_id`.
            #
            # `slot_strategy_version`: `definition` here is always the
            # freshly reconstructed, un-promoted (version=1) catalog
            # object (`candidates`, never `promoted`) -- but every trial
            # this task has ever placed (before or after this milestone)
            # used the PROMOTED version as its own identity's
            # `strategy_version` (the pre-existing `recurring_strategy_
            # paper_trial_seed` call sites already relied on this).
            # `promoted_trial_slot_version` answers "what version would
            # this candidate's slot use once promoted" as a pure,
            # deterministic fact about the catalog itself -- it does not
            # require today's promotion to actually succeed, which is
            # exactly the point: a slot must stay findable for closing
            # even on a cycle where fresh promotion fails. The Operator
            # Console read model computes this identical value the same
            # way -- see that function's own docstring for why drift here
            # would be a real bug, not a cosmetic one.
            slot_strategy_version = promoted_trial_slot_version(
                definition_version=definition.version
            )
            if existing_state is not None:
                self._adopt_legacy_strategy_paper_trial_if_needed(
                    definition=definition,
                    strategy_version=slot_strategy_version,
                    instrument_id=instrument_id,
                    existing_state=existing_state,
                    open_orders_by_strategy=open_orders_by_strategy,
                )
            slot_trials = self._strategy_paper_trials.for_slot(
                strategy_id=definition.strategy_id,
                strategy_version=slot_strategy_version,
                instrument_id=instrument_id,
            )
            slot_trials_by_strategy[definition.strategy_id] = slot_trials
            latest_trial = slot_trials[-1] if slot_trials else None
            latest_closure = (
                None
                if latest_trial is None
                else self._strategy_paper_trial_closures.for_trial(trial_id=latest_trial.trial_id)
            )
            if latest_trial is not None and latest_closure is None:
                active_instrument_ids.add(instrument_id)
                replayed.append(latest_trial.opening_authorization_id)
                evaluated = evaluated_by_strategy.get(definition.strategy_id)
                if existing_state is not None and evaluated is not None:
                    close_definition, close_evaluation = evaluated
                    close_output, close_rejection = self._submit_strategy_paper_trial_close(
                        definition=close_definition,
                        evaluation=close_evaluation,
                        active_trial=latest_trial,
                        existing_state=existing_state,
                        universe=universe,
                        observation_result=observation_result,
                        plan=plan,
                        cycle_id=cycle_id,
                        as_of=as_of,
                        executed_at=executed_at,
                    )
                    if close_output is not None:
                        outputs.append(close_output)
                    if close_rejection is not None:
                        rejected.append(close_rejection)
                elif existing_state is not None:
                    rejected.append(
                        f"{definition.strategy_id}:CANNOT_EVALUATE_FOR_CLOSE_THIS_CYCLE"
                    )
                continue
            promoted_pair = promoted_by_strategy_id.get(definition.strategy_id)
            if promoted_pair is None:
                # Did not qualify this cycle -- already recorded above;
                # never a retrial candidate regardless of cooldown state.
                continue
            eligibility_state = evaluate_retrial_eligibility(
                latest_trial=latest_trial, latest_closure=latest_closure, as_of=as_of
            )
            if eligibility_state in (
                TrialRetrialEligibility.NO_PRIOR_TRIAL,
                TrialRetrialEligibility.ELIGIBLE_FOR_NEXT_GENERATION,
            ):
                retrial_eligible.append(promoted_pair)
            else:
                rejected.append(f"{definition.strategy_id}:{eligibility_state.value}")

        # A candidate that is itself eligible for a fresh trial can still
        # lose to ANOTHER candidate's currently-active trial on the SAME
        # instrument -- domain.crypto_paper.MAX_OPEN_ORDERS_PER_PRODUCT=1
        # structurally backs this up regardless, but this records the exact
        # documented reason (docs/OPERATOR_GUIDE.md's own
        # "ALREADY_CONSIDERED_ANOTHER_CANDIDATE_ALREADY_OPEN_THIS_INSTRUMENT")
        # rather than a generic placement failure.
        final_retrial_eligible: list[tuple[StrategyDefinition, StrategyEvaluationResult]] = []
        for definition, evaluation in retrial_eligible:
            if definition.instrument_ids[0] in active_instrument_ids:
                rejected.append(
                    f"{definition.strategy_id}:ALREADY_CONSIDERED_"
                    "ANOTHER_CANDIDATE_ALREADY_OPEN_THIS_INSTRUMENT"
                )
            else:
                final_retrial_eligible.append((definition, evaluation))

        # ---- Phase E: rank + bound-select at most one NEW trial this
        # cycle, among candidates genuinely eligible for a fresh generation.
        # Placing one genuinely requires fresh contemporaneous evidence
        # (Phase C); ranking itself does not (prioritize() never reads a
        # projection's reference_price), so it still runs -- and still
        # honestly records who lost the ranking -- even when evidence is
        # unavailable, only actual placement is gated.
        selected: tuple[StrategyDefinition, StrategyEvaluationResult] | None = None
        projection_by_strategy_id: dict[str, SharedOpportunityProjection] = {}
        if final_retrial_eligible:
            placeholder_price = (
                price_evidence_from_live_observation(
                    observation_result.observation, product_id=BENCHMARK_INSTRUMENT
                ).price
                if observation_result.disposition is ContemporaneousObservationDisposition.SATISFIED
                and observation_result.observation is not None
                else Decimal(1)
            )
            projections = [
                build_strategy_opportunity_projection(
                    definition=definition,
                    evaluation=evaluation,
                    reference_price=placeholder_price,
                    generated_at=executed_at,
                    opportunity_ordinal=f"{cycle_id}-recurring",
                )
                for definition, evaluation in final_retrial_eligible
            ]
            for (definition, _evaluation), projection in zip(
                final_retrial_eligible, projections, strict=True
            ):
                projection_by_strategy_id[definition.strategy_id] = projection
            decisions = tuple(
                evaluate_eligibility(item, evaluated_at=executed_at) for item in projections
            )
            priorities = prioritize(
                tuple(projections),
                decisions,
                opportunity_set_id=f"{cycle_id}-strategy-paper-trial",
                health_by_method=self._strategy_paper_health_by_method(),
            )
            strategy_id_by_projection_id = {
                item.projection_id: item.strategy_or_method_id for item in projections
            }
            rank_by_strategy_id = {
                strategy_id_by_projection_id[item.projection_id]: item.rank for item in priorities
            }
            ranked = sorted(
                final_retrial_eligible,
                key=lambda pair: rank_by_strategy_id.get(
                    pair[0].strategy_id, len(final_retrial_eligible) + 1
                ),
            )
            selection = ranked[:MAX_NEW_STRATEGY_PAPER_TRIALS_PER_CYCLE]
            selected_ids = {item[0].strategy_id for item in selection}
            for definition, _evaluation in ranked:
                if definition.strategy_id not in selected_ids:
                    rejected.append(
                        f"{definition.strategy_id}:NOT_SELECTED_THIS_CYCLE_"
                        "RANK_OR_SAME_INSTRUMENT_CAPACITY"
                    )
            selected = selection[0] if selection else None

        # ---- Phase B/C: place the selected candidate's genuinely new
        # trial generation -- requires SATISFIED contemporaneous evidence;
        # fails closed (abstains) on STALE/ABSENT rather than a silent
        # direct Coinbase fallback (fallback_source is never supplied).
        if selected is not None:
            definition, _evaluation = selected
            instrument_id = definition.instrument_ids[0]
            if (
                observation_result.disposition
                is not ContemporaneousObservationDisposition.SATISFIED
            ):
                rejected.append(
                    f"{definition.strategy_id}:{observation_result.disposition.value}_"
                    "CONTEMPORANEOUS_EVIDENCE"
                )
            else:
                assert observation_result.observation is not None
                contract = self._crypto_contract(
                    instrument_id, universe=universe, executed_at=executed_at
                )
                next_generation = next_trial_generation(
                    trials=slot_trials_by_strategy.get(definition.strategy_id, ())
                )
                seed = strategy_paper_trial_id(
                    strategy_id=definition.strategy_id,
                    strategy_version=definition.version,
                    instrument_id=instrument_id,
                    generation=next_generation,
                )
                authorization_id = f"{seed}-authorization"
                if universe is None or contract is None:
                    rejected.append(f"{definition.strategy_id}:CONTRACT_OR_UNIVERSE_UNAVAILABLE")
                else:
                    decision_price_evidence = price_evidence_from_live_observation(
                        observation_result.observation, product_id=instrument_id
                    )
                    projection = projection_by_strategy_id[definition.strategy_id]
                    try:
                        placement = ExecuteStrategyPaperTrial(self._crypto_paper).place_order(
                            definition=definition,
                            projection=projection,
                            contract=contract,
                            universe=universe,
                            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                            price_evidence=decision_price_evidence,
                            operating_cycle_enabled=plan.paper_eligible,
                            executed_at=executed_at,
                            actor=STRATEGY_PAPER_TRIAL_ACTOR,
                            idempotency_seed=seed,
                        )
                    except (StrategyPaperTrialError, CryptoPaperError) as error:
                        rejected.append(
                            f"{definition.strategy_id}:PLACEMENT_FAILED:"
                            f"{type(error).__name__}:{error}"
                        )
                    else:
                        outputs.append(authorization_id)
                        if placement.order is not None:
                            trials_created += 1
                            if placement.intent is not None:
                                outputs.append(placement.intent.intent_id)
                            if placement.preview is not None:
                                outputs.append(placement.preview.preview_id)
                            outputs.append(placement.order.order_id)
                            # Repeatable Strategy PAPER Evidence v1: durably
                            # retain this generation's own trial-open record.
                            # The instrument this generation targets is
                            # structurally guaranteed to have had no active
                            # trial this cycle (final_retrial_eligible
                            # excludes every instrument in
                            # active_instrument_ids above), so no OTHER
                            # candidate's close this same cycle could have
                            # touched this product -- existing_state's own
                            # (pre-Phase-F) position snapshot for it is
                            # still current, making it a safe, correct
                            # baseline source. ``existing_state`` can itself
                            # be ``None`` (a genuinely fresh account with no
                            # state snapshot written yet) -- guarded rather
                            # than assumed.
                            baseline_position = (
                                None
                                if existing_state is None
                                else next(
                                    (
                                        item
                                        for item in existing_state.positions
                                        if item.product_id == instrument_id
                                    ),
                                    None,
                                )
                            )
                            baseline = (
                                Decimal(0)
                                if baseline_position is None
                                else baseline_position.realized_paper_pnl
                            )
                            trial_record = build_strategy_paper_trial_record(
                                strategy_id=definition.strategy_id,
                                strategy_version=definition.version,
                                instrument_id=instrument_id,
                                account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                                generation=next_generation,
                                opened_at=executed_at,
                                opening_authorization_id=placement.order.authorization_id,
                                opening_order_id=placement.order.order_id,
                                opening_realized_pnl_baseline=baseline,
                            )
                            # Strategy Version Provenance v1: the trial row
                            # and its binding to this cycle's admission
                            # transition are written in ONE transaction; the
                            # database rejects a non-legacy trial without it.
                            RecordProvenancedTrialOpened(self._strategy_provenance).execute(
                                trial=trial_record,
                                transition_id=admission_transition_ids[definition.strategy_id],
                            )
                        elif placement.eligibility.state is not PaperEligibilityState.ELIGIBLE:
                            rejected.append(
                                f"{definition.strategy_id}:"
                                f"{'|'.join(placement.eligibility.reasons)}"
                            )
                        elif placement.preview is not None and placement.preview.rejection_reasons:
                            rejected.append(
                                f"{definition.strategy_id}:PREVIEW:"
                                f"{'|'.join(placement.preview.rejection_reasons)}"
                            )
                        else:
                            rejected.append(f"{definition.strategy_id}:PLACEMENT_REJECTED")

        # ---- Phase F: unconditional lifecycle re-scan of every currently
        # open order on this account -- mirrors _execute_crypto_paper_trial
        # exactly. Fill evidence is the SAME canonical observation obtained
        # above (Phase C); never a silent direct-provider fallback.
        account_state = self._crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        fills_applied = 0
        expired_count = 0
        if account_state is not None:
            for order in account_state.orders:
                if order.state not in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}:
                    continue
                if executed_at >= order.valid_until:
                    ExpireCryptoPaperOrder(self._crypto_paper).execute(
                        account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                        order_id=order.order_id,
                        expired_at=executed_at,
                        actor=STRATEGY_PAPER_TRIAL_ACTOR,
                        idempotency_key=f"{order.order_id}-expire-{cycle_id}",
                    )
                    expired_count += 1
                    outputs.append(order.order_id)
                    continue
                if (
                    observation_result.disposition
                    is not ContemporaneousObservationDisposition.SATISFIED
                    or observation_result.observation is None
                    or observation_result.observation.captured_at <= order.submitted_at
                ):
                    # A fill must be justified by evidence observed strictly
                    # after the order was submitted; an order submitted this
                    # cycle is only fillable starting a later cycle (mirrors
                    # _execute_crypto_paper_trial's identical rule).
                    continue
                fill_evidence = price_evidence_from_live_observation(
                    observation_result.observation, product_id=order.product_id
                )
                fill_id = f"{order.order_id}-fill-{cycle_id}"
                applied = ApplyCryptoPaperMarketEvidence(self._crypto_paper).execute(
                    account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                    order_id=order.order_id,
                    fill_id=fill_id,
                    evidence=fill_evidence,
                    fee_bps=CONSERVATIVE_CRYPTO_FEE_BPS,
                    slippage_bps=CONSERVATIVE_CRYPTO_SLIPPAGE_BPS,
                    actor=STRATEGY_PAPER_TRIAL_ACTOR,
                    idempotency_key=f"{fill_id}-key",
                )
                if applied.value is not None:
                    outputs.append(fill_id)
                    if not applied.idempotent_replay:
                        fills_applied += 1

        # ---- Phase T' (Repeatable Strategy PAPER Evidence v1): finalize
        # any trial whose lifecycle Phase F just resolved -- either a
        # confirmed-filled closing SELL (genuine TRIAL_HORIZON_EXIT
        # evidence) or an opening order that just expired without ever
        # filling (OPENING_ORDER_EXPIRED_UNFILLED). ``account_state`` above
        # was fetched ONCE, before Phase F's own loop -- Phase F applies
        # fills/expiries through the repository directly and never
        # reassigns that local snapshot, so it is stale by the time Phase F
        # finishes. Re-fetch here so this phase genuinely observes this
        # cycle's own fills/expiries rather than the pre-Phase-F state.
        account_state = self._crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        trials_closed = 0
        if account_state is not None:
            # Iterates every slot Phase T already looked up above (the full
            # canonical catalog, not `promoted`) -- finalizing a closure
            # must never depend on this cycle's own promotion outcome
            # either, for the identical reason submitting it doesn't.
            for slot_history in slot_trials_by_strategy.values():
                if not slot_history:
                    continue
                latest_trial = slot_history[-1]
                if (
                    self._strategy_paper_trial_closures.for_trial(trial_id=latest_trial.trial_id)
                    is not None
                ):
                    continue  # already terminal -- nothing to finalize
                closure_output = self._finalize_strategy_paper_trial_closure(
                    active_trial=latest_trial,
                    account_state=account_state,
                    executed_at=executed_at,
                )
                if closure_output is not None:
                    outputs.append(closure_output)
                    trials_closed += 1

        # Recurring Strategy PAPER Intraday Execution Correction v1: unlike
        # every other id in this method, ``reconciliation_id`` was scoped to
        # ``cycle_id`` alone -- harmless while this task genuinely dispatched
        # at most once per cycle_id, but once a cycle_id can span many
        # genuine dispatches (see ``awaits_intraday_continuation`` above), a
        # second same-cycle_id call reused the identical ``idempotency_key``
        # and ``ReconcileCryptoPaperAccount`` silently replayed the FIRST
        # dispatch's now-stale reconciliation record rather than genuinely
        # re-evaluating current account state -- reconciliation would never
        # progress intraday at all. Folding ``executed_at`` in makes every
        # genuine dispatch reconcile fresh, exactly matching this call's own
        # "unconditional lifecycle re-scan" contract immediately above.
        reconciliation_id = (
            f"{cycle_id}-{executed_at.strftime('%Y%m%dT%H%M%S%f')}-strategy-paper-reconciliation"
        )
        reconciliation = ReconcileCryptoPaperAccount(self._crypto_paper).execute(
            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
            reconciliation_id=reconciliation_id,
            evaluated_at=executed_at,
            order_id=None,
            idempotency_key=f"{reconciliation_id}-key",
        )
        outputs.append(reconciliation.value.reconciliation_id)

        # ---- Phase D/G: automatically materialize attribution + strategy
        # health for every tracked order on this account, open or closed.
        # The real retained TotalMarketContextSnapshot (if any) is re-fetched
        # here -- never fabricated -- exactly mirroring how CONTEXT_
        # ENRICHMENT_REBUILD already reads state.total_market_snapshot_id.
        consideration_context_snapshot = (
            None
            if state.total_market_snapshot_id is None
            else self._context.context_snapshot(state.total_market_snapshot_id)
        )
        attribution_ids, health_ids = self._materialize_strategy_paper_performance(
            trials=tuple(
                trial for history in slot_trials_by_strategy.values() for trial in history
            ),
            cycle_id=cycle_id,
            as_of=as_of,
            executed_at=executed_at,
            candle_history=candle_history,
            total_market_snapshot_id=state.total_market_snapshot_id,
            consideration_context=(
                None
                if consideration_context_snapshot is None
                else consideration_context_snapshot.overall_state.value
            ),
        )
        outputs.extend(attribution_ids)
        outputs.extend(health_ids)
        outputs.extend(retained_evaluation_ids)

        mutated = bool(
            trials_created
            or trials_closed
            or fills_applied
            or expired_count
            or not reconciliation.idempotent_replay
        )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED if mutated else OperatingTaskStatus.REUSED,
            tuple(outputs),
            (*replayed, *replayed_evaluation_ids),
            mutated,
            0,
            (
                f"STRATEGY_PAPER_CANDIDATES_CONSIDERED:{len(candidates)}",
                f"STRATEGY_PAPER_CANDIDATES_PROMOTED:{len(promoted)}",
                f"STRATEGY_PAPER_TRIALS_CREATED:{trials_created}",
                f"STRATEGY_PAPER_TRIALS_CLOSED:{trials_closed}",
                f"STRATEGY_PAPER_FILLS_APPLIED:{fills_applied}",
                f"STRATEGY_PAPER_ORDERS_EXPIRED:{expired_count}",
                f"STRATEGY_PAPER_RECONCILIATION:{reconciliation.value.status.value}",
                f"STRATEGY_PAPER_ATTRIBUTIONS:{len(attribution_ids)}",
                f"STRATEGY_PAPER_HEALTH_ASSESSMENTS:{len(health_ids)}",
                f"STRATEGY_PAPER_EVALUATIONS_RETAINED:{len(retained_evaluation_ids)}",
                f"STRATEGY_PAPER_PROVENANCE_ADMISSIONS_RETAINED:{admissions_retained}",
                f"STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:{observation_result.disposition.value}",
                "STRATEGY_PAPER_TOTAL_MARKET_CONTEXT:"
                f"{state.total_market_snapshot_id or 'UNAVAILABLE'}",
                *(f"STRATEGY_PAPER_REJECTED:{item}" for item in rejected),
                *warnings,
                "PAPER_ONLY",
                "NO_COINBASE_ORDER_SUBMISSION",
                "LIVE_AUTHORITY_NONE",
            ),
            # Recurring Strategy PAPER Intraday Execution Correction v1:
            # unconditional, every dispatch -- see this method's own
            # docstring and TaskExecution.awaits_intraday_continuation's.
            # Unlike COUNTERFACTUAL_FOLLOWUP_REFRESH's bounded-backlog use of
            # this same field, a continuously managed open PAPER position
            # never "drains"; only a genuine exception (handled generically
            # by _dispatch_task, not here) ever freezes this task for the
            # rest of the UTC day.
            awaits_intraday_continuation=True,
        )

    def _materialize_strategy_paper_performance(
        self,
        *,
        trials: tuple[StrategyPaperTrialRecord, ...],
        cycle_id: str,
        as_of: datetime,
        executed_at: datetime,
        candle_history: tuple[CryptoDailyCandle, ...],
        total_market_snapshot_id: str | None,
        consideration_context: str | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Automatic post-trial attribution + strategy health (Phase G), for
        every order this account currently tracks, open or closed. Reuses
        the existing, unmodified ``MaterializePerformanceAttribution``/
        ``MaterializeStrategyHealth`` application services completely
        unchanged -- via one small, in-memory-backed local source, mirroring
        the same "small local Protocol-double adapter" idiom already used
        immediately above for ``COUNTERFACTUAL_FOLLOWUP_REFRESH``'s own
        ``_BoundedKalshiFollowUpSource`` -- rather than a second, competing
        producer wired into ``adapters.performance_attribution_sources``.

        Recurring Strategy PAPER Intraday Execution Correction v1: this
        method can now genuinely run many times within one cycle_id (see
        ``awaits_intraday_continuation`` above), so two identity choices
        that were harmless under the old at-most-once-per-cycle_id
        assumption needed to change, both discovered via this milestone's
        own real-Postgres same-cycle_id regression test:

        * a later same-day tick where nothing about a tracked order
          genuinely changed reproduces the IDENTICAL attribution/health
          content: unchanged sources are reused by ``(source_id,
          source_hash)``. (PAPER Trial Closure Readiness v1 superseded the
          original "stamp everything with the day-scoped ``as_of``" choice:
          a genuinely NEW row is now stamped with ``executed_at`` so same-day
          OPEN -> CLOSED supersession is temporally ordered -- see the
          comment at the ``MaterializePerformanceAttribution`` call below.)
        * ``run_id`` folds in a digest of exactly the ``(source_id,
          source_hash)`` pairs this dispatch is considering, rather than
          ``cycle_id`` alone -- so it stays stable (a safe, ordinary replay)
          across ticks where no tracked order's state genuinely progressed,
          while still minting a genuinely new, non-colliding id the moment
          any order's state does progress (e.g. a fill or expiry). Without
          this, a second same-cycle_id dispatch that genuinely closed or
          filled an order reused the exact same ``attribution_id``/
          ``health_id`` as the first dispatch's now-stale content and
          ``MaterializePerformanceAttribution``/``MaterializeStrategyHealth``
          correctly, deliberately raised a hard identity conflict rather
          than silently overwrite -- attribution/health could never
          progress intraday at all.

        Threads ``total_market_snapshot_id`` (this cycle's own retained
        ``TotalMarketContextSnapshot``, resolved once per cycle by
        ``PostgresOperatingStateSource`` exactly like
        ``CONTEXT_ENRICHMENT_REBUILD`` already consumes it) into
        ``context_at_consideration_id`` (Phase D) -- never fabricated:
        ``None`` when no eligible context exists yet, which
        ``performance_attribution.assess_strategy_health`` already turns
        into an honest ``DATA_QUALITY_BLOCKED`` rather than inventing
        evidence.

        Expected-return figures are recomputed fresh from currently-retained
        candle history rather than replayed from the original decision-time
        evaluation (this milestone does not separately persist
        ``StrategyEvaluationResult``) -- an explicit, documented limitation,
        never silently presented as the original decision-time figure.

        ``MaterializeStrategyHealth`` is deliberately called unscoped (like
        ``PERFORMANCE_ATTRIBUTION_REBUILD``'s own existing call), so it also
        recomputes the shared venue/portfolio summaries every retained
        GENUINE_PAPER attribution already feeds -- an accepted, idempotent,
        harmless overlap with that other task's own identical call, not a
        new coordination mechanism.
        """

        account_state = self._crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        if account_state is None:
            return (), ()

        reference_candidates = canonical_strategy_benchmarks(created_at=executed_at)
        # PAPER Trial Closure Readiness v1 (F3/F4): an order that belongs to
        # a bounded trial is economically closed exactly when that trial has
        # a TRIAL_HORIZON_EXIT closure -- the durable record written only
        # once the closing SELL genuinely filled the position flat, naming
        # the exact opening and closing orders. Deriving it from the shared,
        # per-product position row instead would be wrong across
        # generations (the row is reopened by generation N+1's BUY) and
        # would mark every EXPIRED close attempt's SELL closed.
        trial_order_ids: set[str] = set()
        closed_at_by_order_id: dict[str, datetime] = {}
        for trial in trials:
            trial_order_ids.add(trial.opening_order_id)
            trial_order_ids.update(
                item.order_id
                for item in account_state.orders
                if item.order_id.startswith(f"{trial.trial_id}-close")
            )
            trial_closure = self._strategy_paper_trial_closures.for_trial(trial_id=trial.trial_id)
            if (
                trial_closure is None
                or trial_closure.terminal_reason
                is not StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT
            ):
                continue
            closing_fills = [
                item.event_time
                for item in account_state.fills
                if item.order_id == trial_closure.closing_order_id
            ]
            economic_close_at = max(closing_fills) if closing_fills else trial_closure.closed_at
            closed_at_by_order_id[trial.opening_order_id] = economic_close_at
            closed_at_by_order_id[trial_closure.closing_order_id] = economic_close_at
        snapshots: dict[str, AttributionSourceSnapshot] = {}
        for order in account_state.orders:
            authorization = next(
                (
                    item
                    for item in account_state.authorizations
                    if item.authorization_id == order.authorization_id
                ),
                None,
            )
            if authorization is None:
                continue
            definition = next(
                (
                    item
                    for item in reference_candidates
                    if item.strategy_id == authorization.strategy_or_discovery_id
                ),
                None,
            )
            if definition is None:
                # Not one of the recognized recurring candidates -- never
                # guess which strategy placed it.
                continue
            preview = next(
                (item for item in account_state.previews if item.preview_id == order.preview_id),
                None,
            )
            if preview is None:
                continue

            evaluation: StrategyEvaluationResult | None = None
            if len(candle_history) >= definition.evidence_requirements.minimum_history_days:
                try:
                    backtest = simulate_strategy_backtest(
                        definition,
                        candle_history,
                        evidence_class=candle_history[0].evidence_class,
                        evidence_provenance=candle_history[0].capture_id,
                    )
                except StrategyBenchmarkError:
                    evaluation = None
                else:
                    evaluation = evaluate_external_strategy(definition, backtest)
            if evaluation is None:
                # Cannot honestly attribute expected-return evidence without
                # a real evaluation -- retry on a later cycle rather than
                # inventing one.
                continue

            fill_rows = tuple(
                item for item in account_state.fills if item.order_id == order.order_id
            )
            fill_application: CryptoPaperFillApplication | None = None
            # Repeatable Strategy PAPER Evidence v1: declared here (default
            # None), not only inside `if fill_rows:` below, since the
            # position_closed lookup further down needs it for EVERY
            # order, not just ones with at least one retained fill.
            position: CryptoPaperPosition | None = None
            if fill_rows:
                latest_fill = max(fill_rows, key=lambda item: item.event_time)
                fill_reservation = next(
                    (
                        item
                        for item in account_state.reservations
                        if item.order_id == order.order_id
                    ),
                    None,
                )
                position = next(
                    (
                        item
                        for item in account_state.positions
                        if item.product_id == order.product_id
                    ),
                    None,
                )
                event = next(
                    (item for item in account_state.events if item.order_id == order.order_id),
                    None,
                )
                journal = next(
                    (
                        item
                        for item in account_state.journals
                        if item.journal_id == primary_fill_journal_id(latest_fill.fill_id)
                    ),
                    None,
                )
                if (
                    fill_reservation is not None
                    and position is not None
                    and event is not None
                    and journal is not None
                ):
                    fill_application = CryptoPaperFillApplication(
                        order=order,
                        reservation=fill_reservation,
                        account_state=account_state.account_state,
                        position=position,
                        fill=latest_fill,
                        event=event,
                        journal=journal,
                    )

            closure: CryptoPaperCancellation | None = None
            if order.state in {PaperOrderState.EXPIRED, PaperOrderState.CANCELED}:
                closure_reservation = next(
                    (
                        item
                        for item in account_state.reservations
                        if item.order_id == order.order_id
                    ),
                    None,
                )
                if closure_reservation is not None:
                    closure = CryptoPaperCancellation(
                        order=order,
                        reservation=closure_reservation,
                        account_state=account_state.account_state,
                        events=tuple(
                            item for item in account_state.events if item.order_id == order.order_id
                        ),
                    )

            reconciliation_for_order = next(
                (
                    item
                    for item in reversed(account_state.reconciliations)
                    if item.order_id in {None, order.order_id}
                ),
                None,
            )

            # ``eligibility``/``definition_id`` are required fields of
            # StrategyPaperOrderPlacement, but build_attribution_source_
            # from_trial never reads either (confirmed: only .order/
            # .preview/.projection_id are) -- an inert structural
            # placeholder, not a fact anything downstream relies on.
            placeholder_eligibility = CryptoPaperEligibility(
                decision_id=f"{order.order_id}-recurring-attribution",
                opportunity_set_id=cycle_id,
                projection_id=authorization.projection_id,
                policy_id=CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1,
                evaluated_at=executed_at,
                state=PaperEligibilityState.ELIGIBLE,
                reasons=(),
                product_contract_id=None,
                account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
                canonical_hash=content_digest(
                    (order.order_id, "recurring-attribution-placeholder")
                ),
            )
            placement = StrategyPaperOrderPlacement(
                definition_id=definition.strategy_id,
                projection_id=authorization.projection_id,
                eligibility=placeholder_eligibility,
                intent=None,
                preview=preview,
                order=order,
            )
            outcome = StrategyPaperTrialOutcome(
                placement=placement,
                fill=fill_application,
                closure=closure,
                reconciliation=reconciliation_for_order,
            )
            snapshot = build_attribution_source_from_trial(
                definition=definition,
                evaluation=evaluation,
                outcome=outcome,
                materialized_at=as_of,
                context_at_consideration_id=total_market_snapshot_id,
                consideration_context=consideration_context,
                # Repeatable Strategy PAPER Evidence v1: `closure` above
                # only ever reflects an EXPIRED/CANCELED order (see its own
                # comment) -- a FILLED closing SELL that genuinely zeroed
                # out the position is a real, independently-observed fact
                # from `position` (already looked up above for
                # `fill_application`), not something `closure` itself ever
                # represents.
                position_closed=(
                    order.order_id in closed_at_by_order_id
                    if order.order_id in trial_order_ids
                    else position is not None and position.state == "CLOSED"
                ),
                closed_at=closed_at_by_order_id.get(order.order_id),
            )
            snapshots[snapshot.source_id] = snapshot

        if not snapshots:
            return (), ()

        class _RecurringStrategySnapshotSource:
            def snapshots(
                self, evidence_class: PaperEvidenceClass
            ) -> tuple[AttributionSourceSnapshot, ...]:
                if evidence_class is not PaperEvidenceClass.GENUINE_PAPER:
                    return ()
                return tuple(sorted(snapshots.values(), key=lambda item: item.source_id))

            def snapshot(
                self, source_id: str, evidence_class: PaperEvidenceClass
            ) -> AttributionSourceSnapshot | None:
                if evidence_class is not PaperEvidenceClass.GENUINE_PAPER:
                    return None
                return snapshots.get(source_id)

        # Stable across ticks where nothing tracked genuinely changed (same
        # (source_id, source_hash) pairs -> same digest -> same run_id/ids,
        # so MaterializePerformanceAttribution/MaterializeStrategyHealth
        # correctly recognize an ordinary replay); a fresh, non-colliding
        # run_id the moment any tracked order's state genuinely progresses.
        # See this method's own docstring above.
        content_seed = content_digest(
            tuple(sorted((item.source_id, item.source_hash) for item in snapshots.values()))
        )
        run_id = f"{cycle_id}-{content_seed}-strategy-performance"
        # PAPER Trial Closure Readiness v1: a NEW attribution row is stamped
        # with this tick's real ``executed_at``, never the day-scoped
        # ``as_of`` -- otherwise a same-day OPEN row and its superseding
        # CLOSED row (e.g. the closing SELL, submitted at 00:00 and filled
        # at 00:05) shared one ``materialized_at`` and "current" was decided
        # by an attribution-id hash tie-break with no temporal meaning.
        # Unchanged sources are still REUSED by (source_id, source_hash), so
        # a later tick where nothing changed mints nothing new. Health's
        # ``as_of`` is the later of the cycle day and the newest attribution
        # it covers: content-determined, so unchanged ticks replay the
        # identical health row, and never earlier than its own evidence.
        attribution_result = MaterializePerformanceAttribution(
            self._performance, _RecurringStrategySnapshotSource()
        ).execute(
            run_id=run_id,
            evidence_class=PaperEvidenceClass.GENUINE_PAPER,
            materialized_at=executed_at,
            idempotency_key=f"{run_id}-key",
        )
        health_as_of = max(
            (as_of, *(item.materialized_at for item in attribution_result.attributions))
        )
        health_result = MaterializeStrategyHealth(self._performance).execute(
            run_id=run_id,
            evidence_class=PaperEvidenceClass.GENUINE_PAPER,
            as_of=health_as_of,
            idempotency_key=f"{cycle_id}-{content_seed}-strategy-health-key",
        )
        strategy_ids = {item.strategy_or_method_id for item in snapshots.values()}
        health_ids = tuple(
            item.health_id
            for item in health_result.assessments
            if item.strategy_or_method_id in strategy_ids
        )
        return (
            tuple(item.attribution_id for item in attribution_result.attributions),
            health_ids,
        )
