"""Composition root for the only Apex command: the offline golden simulation."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256
from json import dumps
from typing import TYPE_CHECKING, Any, Protocol, cast

from apex import ENGINE_VERSION
from apex.adapters.autopilot_fixture import (
    GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID,
    EvidenceReviewFixtureMarketClient,
    fixture_autopilot_registry,
    fixture_governed_evidence_registry,
    fixture_governed_forward_prediction,
    fixture_paper_portfolio_registry,
)
from apex.adapters.canonical_report import (
    CanonicalReportRenderer,
    report_document,
    report_sha256,
)
from apex.adapters.fixtures import (
    REGISTERED_FIXTURE_IDS,
    ClassifiedRuleBoundSettlementFixture,
    DecisionFixture,
    FixtureReader,
    PartialFillCanceledExecutionScriptFixture,
    RuleBoundSettlementFixture,
    SettlementFixture,
    UnfilledExpiryExecutionScriptFixture,
    VoidContractRuleFixture,
)
from apex.adapters.kalshi_capture_fixtures import (
    MARKET_TICKER as CAPTURE_FIXTURE_MARKET_TICKER,
)
from apex.adapters.kalshi_capture_fixtures import (
    KalshiFixtureOrderbookCaptureClient,
    VirtualCaptureClock,
)
from apex.adapters.kalshi_comparison_fixtures import temporal_fixture_scans
from apex.adapters.kalshi_complement_fixtures import (
    KalshiFixtureBinaryComplementClient,
    fixture_market,
)
from apex.adapters.kalshi_consistency_fixtures import KalshiFixturePublicConsistencyClient
from apex.adapters.kalshi_history_fixtures import temporal_history_fixture_runs
from apex.adapters.kalshi_market_fixtures import KalshiFixtureMarketDataClient
from apex.adapters.kalshi_orderbook_history_fixtures import (
    MARKET_TICKER as ORDERBOOK_HISTORY_MARKET_TICKER,
)
from apex.adapters.kalshi_orderbook_history_fixtures import (
    temporal_orderbook_history_fixture,
)
from apex.adapters.kalshi_public import (
    KalshiPublicEventDataClient,
    KalshiPublicEventNormalizer,
    KalshiPublicMarketDataClient,
    KalshiPublicMarketNormalizer,
    KalshiPublicOrderbookDataClient,
)
from apex.adapters.kalshi_sweep_fixtures import (
    KalshiFixtureOrderbookSweepClient,
    fixture_capture_candidates,
)
from apex.adapters.kalshi_threshold_fixtures import threshold_ladder_fixture
from apex.adapters.simulator import DeterministicSimulator
from apex.application.authorize_order import (
    AuthorizationUnitOfWork,
    CampaignInitializationCommand,
    DurableAuthorizationService,
)
from apex.application.autopilot import (
    AutopilotCycleAuthority,
    AutopilotCycleCommand,
    AutopilotCycleResult,
    AutopilotError,
    CampaignControlError,
    RunAutopilotShadowCycle,
    StrategyRegistry,
    governed_strategy_descriptors,
)
from apex.application.build_event_observation_history import (
    BuildEventObservationHistory,
    EventHistoryReport,
    EventHistoryRequest,
    EventObservationHistoryError,
    InMemoryEventObservationHistoryStore,
)
from apex.application.build_orderbook_observation_history import (
    BuildOrderbookObservationHistory,
    InMemoryOrderbookObservationHistoryStore,
    OrderbookHistoryReport,
    OrderbookHistoryRequest,
    OrderbookObservationHistoryError,
)
from apex.application.capture_public_orderbook_session import (
    BoundedOrderbookCaptureRequest,
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    InMemoryPublicOrderbookCaptureStore,
    OrderbookCaptureReport,
    PublicOrderbookCaptureError,
)
from apex.application.compare_public_scans import (
    ComparePublicMarketScans,
    InMemoryPublicScanComparisonStore,
    PublicScanComparisonError,
    PublicScanComparisonReport,
)
from apex.application.discover_public_events import (
    CapturedPublicEventPage,
    DiscoverPublicEventConsistency,
    InMemoryPublicEventConsistencyStore,
    PublicEventConsistencyScanSummary,
    PublicEventConsistencyStore,
    PublicEventDiscoveryError,
    PublicEventPageClient,
    PublicEventScanRequest,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    InMemoryPublicMarketDiscoveryStore,
    PublicMarketDiscoveryError,
    PublicMarketDiscoveryStore,
    PublicMarketPageClient,
    PublicMarketScanRequest,
    PublicMarketScanSummary,
)
from apex.application.interfaces import (
    DecisionInputs,
    ExecutionScript,
    PartialFillCanceledExecutionScript,
    ReferenceEstimateInput,
    RunMetadata,
    SettlementInputs,
    SyntheticExecutionScript,
    UnfilledExpiryExecutionScript,
)
from apex.application.paper_portfolio import (
    PaperCycleCommand,
    PaperPortfolioOperations,
    PaperPositionMarkCommand,
    PaperPositionSettlementCommand,
    RunAutopilotPaperCycle,
)
from apex.application.reconcile_public_outcome_identity import (
    PublicOutcomeReconciliationState,
    RetainedPublicOutcomeIdentity,
    reconcile_public_final_outcome,
)
from apex.application.run_simulation import run_simulation
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    InMemoryPublicBinaryComplementStore,
    PublicBinaryComplementError,
    PublicBinaryComplementReport,
    ScanPublicBinaryComplements,
)
from apex.application.scan_public_threshold_ladders import (
    PublicThresholdLadderScanReport,
    PublicThresholdLadderScanRequest,
    ScanPublicThresholdLadders,
)
from apex.application.strategy_shadow import (
    MARKET_CONSENSUS_BASELINE_STRATEGY_ID,
    OPERATOR_THESIS_STRATEGY_ID,
    OperatorThesisAutopilotStrategy,
    OperatorThesisInput,
    ShadowPredictionState,
    StrategyAuthorityCommand,
    build_market_consensus_baseline_prediction,
    build_operator_thesis_opportunity,
    build_operator_thesis_prediction,
)
from apex.application.sweep_public_kalshi_orderbooks import (
    PublicKalshiOrderbookSweepReport,
    PublicKalshiOrderbookSweepRequest,
    SweepPublicKalshiOrderbooks,
)
from apex.domain.autopilot import (
    CampaignPortfolioPolicy,
    CampaignPortfolioSnapshot,
    EvidenceTier,
)
from apex.domain.campaign import CampaignPolicy
from apex.domain.execution import (
    ClassifiedRuleBoundSettlementFact,
    RuleBoundSettlementFact,
    SettlementFact,
    SettlementOutcome,
)
from apex.domain.market import (
    ContractRuleVersion,
    Instrument,
    MarketSnapshot,
    SourceRecord,
    VoidContractRuleVersion,
    VoidSettlementRuleTerm,
)
from apex.domain.paper import (
    PaperExecutionModel,
    PaperExecutionPolicy,
    PaperUnfilledBehavior,
)
from apex.domain.public_event import evaluate_event_consistency, link_event_markets
from apex.domain.values import (
    ContentHash,
    InformationCutoff,
    Money,
    Price,
    ProbabilityVector,
    Quantity,
    RiskFraction,
    ValueValidationError,
    canonical_json_bytes,
    content_digest,
    deterministic_id,
    parse_decimal,
)

if TYPE_CHECKING:
    from apex.adapters.postgres import PostgresAutopilotStore


def _seed(value: str) -> int:
    if not value.isascii() or not value.isdigit() or (len(value) > 1 and value[0] == "0"):
        raise argparse.ArgumentTypeError("seed must be a canonical non-negative integer")
    return int(value)


def _capital(value: str) -> Money:
    try:
        capital = Money(value)
    except ValueValidationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if capital.amount <= 0:
        raise argparse.ArgumentTypeError("capital must be positive")
    return capital


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Apex offline synthetic golden simulation")
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo-core")
    demo.add_argument("--fixture", choices=REGISTERED_FIXTURE_IDS, required=True)
    demo.add_argument("--capital", type=_capital, required=True)
    demo.add_argument("--seed", type=_seed, required=True)
    fixture_scan = commands.add_parser("scan-kalshi-fixture")
    fixture_scan.add_argument("--persist", action="store_true")
    smoke = commands.add_parser("smoke-kalshi-public")
    smoke.add_argument("--limit", type=int, default=5)
    smoke.add_argument("--max-pages", type=int, default=1)
    smoke.add_argument("--timeout-seconds", type=int, default=10)
    smoke.add_argument("--persist", action="store_true")
    consistency_fixture = commands.add_parser("scan-kalshi-consistency-fixture")
    consistency_fixture.add_argument("--persist", action="store_true")
    consistency = commands.add_parser("smoke-kalshi-consistency")
    consistency.add_argument("--event-limit", type=int, default=5)
    consistency.add_argument("--market-limit", type=int, default=25)
    consistency.add_argument("--max-pages", type=int, default=1)
    consistency.add_argument("--timeout-seconds", type=int, default=10)
    consistency.add_argument("--persist", action="store_true")
    commands.add_parser("scan-kalshi-binary-complements-fixture")
    complements = commands.add_parser("smoke-kalshi-binary-complements")
    complements.add_argument("--market-run-id")
    complements.add_argument("--market-limit", type=int, default=20)
    complements.add_argument("--depth", type=int, default=5)
    complements.add_argument("--max-bundle-contracts", type=str, default="10")
    complements.add_argument("--timeout-seconds", type=int, default=10)
    complements.add_argument("--discover-first", action="store_true")
    commands.add_parser("capture-kalshi-orderbook-series-fixture")
    capture = commands.add_parser("capture-kalshi-orderbook-series")
    capture_group = capture.add_mutually_exclusive_group(required=True)
    capture_group.add_argument("--market-ticker")
    capture_group.add_argument("--latest-open-market", action="store_true")
    capture.add_argument("--discover-first", action="store_true")
    capture.add_argument("--observations", type=int, default=3)
    capture.add_argument("--interval-seconds", type=int, default=10)
    capture.add_argument("--depth", type=int, default=5)
    capture.add_argument("--max-bundle-contracts", default="10")
    capture.add_argument("--timeout-seconds", type=int, default=10)
    commands.add_parser("sweep-kalshi-orderbooks-fixture")
    sweep = commands.add_parser("sweep-kalshi-orderbooks")
    sweep.add_argument("--market-count", type=int, default=12)
    sweep.add_argument("--max-pages", type=int, default=3)
    sweep.add_argument("--market-limit", type=int, default=300)
    sweep.add_argument("--max-enrichments", type=int, default=40)
    sweep.add_argument("--observations", type=int, default=3)
    sweep.add_argument("--interval-seconds", type=int, default=5)
    sweep.add_argument("--depth", type=int, default=5)
    sweep.add_argument("--max-bundle-contracts", default="10")
    sweep.add_argument("--timeout-seconds", type=int, default=10)
    commands.add_parser("scan-kalshi-threshold-ladders-fixture")
    threshold_ladders = commands.add_parser("scan-kalshi-threshold-ladders")
    threshold_ladders.add_argument("--scan-id")
    threshold_ladders.add_argument("--event-count", type=int, default=5)
    threshold_ladders.add_argument("--max-pages", type=int, default=3)
    threshold_ladders.add_argument("--market-limit", type=int, default=300)
    threshold_ladders.add_argument("--max-enrichments", type=int, default=40)
    threshold_ladders.add_argument("--max-markets-per-event", type=int, default=8)
    threshold_ladders.add_argument("--max-relationships", type=int, default=100)
    threshold_ladders.add_argument("--rounds", type=int, default=3)
    threshold_ladders.add_argument("--interval-seconds", type=int, default=10)
    threshold_ladders.add_argument("--depth", type=int, default=5)
    threshold_ladders.add_argument("--max-bundle-contracts", default="10")
    threshold_ladders.add_argument("--max-observation-skew-seconds", type=int, default=5)
    threshold_ladders.add_argument("--timeout-seconds", type=int, default=10)
    diagnostic = commands.add_parser("diagnose-kalshi-capture-selection")
    diagnostic.add_argument("--max-pages", type=int, default=3)
    diagnostic.add_argument("--market-limit", type=int, default=300)
    diagnostic.add_argument("--timeout-seconds", type=int, default=10)
    commands.add_parser("reconcile-kalshi-capture-sessions")
    commands.add_parser("compare-kalshi-scans-fixture")
    comparison = commands.add_parser("compare-kalshi-scans")
    comparison.add_argument("--baseline-run-id", required=True)
    comparison.add_argument("--current-run-id", required=True)
    commands.add_parser("compare-kalshi-latest-scans")
    commands.add_parser("summarize-kalshi-event-history-fixture")
    history = commands.add_parser("summarize-kalshi-event-history")
    history.add_argument("--event-ticker", required=True)
    history.add_argument("--max-runs", type=int, default=20)
    history.add_argument("--first-run-id")
    history.add_argument("--last-run-id")
    history.add_argument("--continuity-gap-seconds", type=int, default=1800)
    commands.add_parser("summarize-kalshi-orderbook-history-fixture")
    orderbook_history = commands.add_parser("summarize-kalshi-orderbook-history")
    orderbook_history.add_argument("--market-ticker", required=True)
    orderbook_history.add_argument("--max-observations", type=int, default=20)
    orderbook_history.add_argument("--first-snapshot-id")
    orderbook_history.add_argument("--last-snapshot-id")
    orderbook_history.add_argument("--continuity-gap-seconds", type=int, default=300)
    shared_materialize = commands.add_parser("shared-opportunity-materialize")
    shared_materialize.add_argument("--opportunity-set-id", required=True)
    shared_materialize.add_argument("--crypto-pulse-id", required=True)
    shared_materialize.add_argument("--kalshi-evaluation-id", action="append", default=[])
    shared_materialize.add_argument("--as-of", required=True)
    shared_materialize.add_argument("--idempotency-key", required=True)
    shared_projection = commands.add_parser("shared-projection-inspect")
    shared_projection.add_argument("--projection-id", required=True)
    shared_eligibility = commands.add_parser("shared-eligibility-inspect")
    shared_eligibility.add_argument("--opportunity-set-id", required=True)
    shared_eligibility.add_argument("--projection-id")
    shared_set = commands.add_parser("shared-opportunity-set-inspect")
    shared_set.add_argument("--opportunity-set-id", required=True)
    shared_priority = commands.add_parser("shared-priority-inspect")
    shared_priority.add_argument("--opportunity-set-id", required=True)
    shared_explanation = commands.add_parser("shared-explanation-inspect")
    shared_explanation.add_argument("--opportunity-set-id", required=True)
    shared_explanation.add_argument("--projection-id")
    shared_allocation = commands.add_parser("shared-allocation-propose")
    shared_allocation.add_argument("--proposal-id", required=True)
    shared_allocation.add_argument("--opportunity-set-id", required=True)
    shared_allocation.add_argument("--idempotency-key", required=True)
    shared_allocation_read = commands.add_parser("shared-allocation-inspect")
    shared_allocation_read.add_argument("--proposal-id", required=True)
    shared_lineage = commands.add_parser("shared-lineage-inspect")
    shared_lineage.add_argument("--projection-id", required=True)
    counterfactual_sync = commands.add_parser("counterfactual-ledger-sync")
    counterfactual_sync.add_argument("--evaluated-at", required=False, default=None)
    counterfactual_inspect = commands.add_parser("counterfactual-ledger-inspect")
    counterfactual_inspect.add_argument("--start", required=False, default=None)
    counterfactual_inspect.add_argument("--end", required=False, default=None)
    counterfactual_inspect.add_argument(
        "--origin",
        required=False,
        default=None,
        choices=("SHARED_OPPORTUNITY_ELIGIBILITY", "INVESTMENT_MANDATE_EVALUATION"),
    )
    counterfactual_inspect.add_argument(
        "--venue", required=False, default=None, choices=("CRYPTO", "KALSHI")
    )
    counterfactual_inspect.add_argument(
        "--disposition",
        required=False,
        default=None,
        choices=("ADVANCED", "CONSTRAINED", "NOT_ADVANCED"),
    )
    counterfactual_decision = commands.add_parser("counterfactual-decision-inspect")
    counterfactual_decision.add_argument("--decision-id", required=True)
    counterfactual_classification_sync = commands.add_parser("counterfactual-classification-sync")
    counterfactual_classification_sync.add_argument(
        "--classifier-version",
        required=False,
        default=None,
        choices=("REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"),
    )
    counterfactual_classification_sync.add_argument("--classified-at", required=False, default=None)
    counterfactual_classification_inspect = commands.add_parser(
        "counterfactual-classification-inspect"
    )
    counterfactual_classification_inspect.add_argument(
        "--classifier-version",
        required=False,
        default=None,
        choices=("REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"),
    )
    counterfactual_classification_inspect.add_argument("--start", required=False, default=None)
    counterfactual_classification_inspect.add_argument("--end", required=False, default=None)
    counterfactual_classification_coverage = commands.add_parser(
        "counterfactual-classification-coverage"
    )
    counterfactual_classification_coverage.add_argument(
        "--classifier-version",
        required=False,
        default=None,
        choices=("REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"),
    )
    followup_plan = commands.add_parser("followup-plan")
    followup_plan.add_argument("--as-of", required=False, default=None)
    followup_plan.add_argument(
        "--policy-version",
        required=False,
        default=None,
        choices=("FOLLOWUP_POLICY_V1", "FOLLOWUP_POLICY_V2"),
        help="Defaults to the current follow-up policy version; pass "
        "FOLLOWUP_POLICY_V1 explicitly for historical backlog inspection.",
    )
    followup_sync = commands.add_parser("followup-sync")
    followup_sync.add_argument("--as-of", required=False, default=None)
    followup_sync.add_argument("--sample-limit", required=False, default=None, type=int)
    followup_sync.add_argument("--timeout-seconds", required=False, default=None, type=int)
    followup_sync.add_argument(
        "--policy-version",
        required=False,
        default=None,
        choices=("FOLLOWUP_POLICY_V1", "FOLLOWUP_POLICY_V2"),
        help="Defaults to the current follow-up policy version; pass "
        "FOLLOWUP_POLICY_V1 explicitly for a manual historical V1 replay.",
    )
    followup_inspect = commands.add_parser("followup-inspect")
    followup_inspect.add_argument(
        "--venue", required=False, default=None, choices=("CRYPTO", "KALSHI")
    )
    followup_inspect.add_argument(
        "--horizon-kind",
        required=False,
        default=None,
        choices=("SHORT_HORIZON", "STRATEGY_HORIZON"),
    )
    followup_inspect.add_argument(
        "--baseline-kind",
        required=False,
        default=None,
        choices=("ORIGIN_EVIDENCE", "DECISION_TIME"),
        help="Unfiltered by default -- shows retained observations under every baseline kind. "
        "ORIGIN_EVIDENCE only ever exists for CRYPTO under a post-V1 policy version.",
    )
    followup_inspect.add_argument(
        "--policy-version",
        required=False,
        default=None,
        choices=("FOLLOWUP_POLICY_V1", "FOLLOWUP_POLICY_V2"),
        help="Unfiltered by default -- shows retained observations under every policy version.",
    )
    followup_inspect.add_argument("--start", required=False, default=None)
    followup_inspect.add_argument("--end", required=False, default=None)
    followup_decision = commands.add_parser("followup-decision-inspect")
    followup_decision.add_argument("--decision-id", required=True)
    followup_evidence_quality_assess = commands.add_parser("followup-evidence-quality-assess")
    followup_evidence_quality_assess.add_argument("--as-of", required=False, default=None)
    followup_evidence_quality_assess.add_argument(
        "--assessment-policy-version",
        required=False,
        default=None,
        choices=("FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1", "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2"),
        help="Defaults to the current evidence-quality assessment policy version.",
    )
    followup_evidence_quality_inspect = commands.add_parser("followup-evidence-quality-inspect")
    followup_evidence_quality_inspect.add_argument(
        "--observation-id",
        required=False,
        default=None,
        help="Show one observation's assessment only; unfiltered by default.",
    )
    followup_evidence_quality_inspect.add_argument(
        "--verdict",
        required=False,
        default=None,
        choices=("ELIGIBLE", "INELIGIBLE_EVIDENCE"),
    )
    followup_evidence_quality_inspect.add_argument(
        "--assessment-policy-version",
        required=False,
        default=None,
        choices=("FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1", "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2"),
        help="Unfiltered by default -- shows retained assessments under every policy version.",
    )
    context_component = commands.add_parser("context-component-inspect")
    context_component.add_argument("--component-id", required=True)
    context_materialize = commands.add_parser("context-snapshot-materialize")
    context_materialize.add_argument("--snapshot-id", required=True)
    context_materialize.add_argument("--source-batch-id", required=True)
    context_materialize.add_argument("--as-of", required=True)
    context_materialize.add_argument("--idempotency-key", required=True)
    context_snapshot = commands.add_parser("context-snapshot-inspect")
    context_snapshot.add_argument("--snapshot-id", required=True)
    commands.add_parser("context-policy-inspect")
    context_attach = commands.add_parser("opportunity-context-attach")
    context_attach.add_argument("--enriched-set-id", required=True)
    context_attach.add_argument("--opportunity-set-id", required=True)
    context_attach.add_argument("--context-snapshot-id", required=True)
    context_attach.add_argument("--attached-at", required=True)
    context_attach.add_argument("--idempotency-key", required=True)
    contextual_explanation = commands.add_parser("contextual-explanation-inspect")
    contextual_explanation.add_argument("--enriched-set-id", required=True)
    contextual_explanation.add_argument("--projection-id")
    context_enriched = commands.add_parser("context-enriched-opportunity-set-inspect")
    context_enriched.add_argument("--enriched-set-id", required=True)
    commands.add_parser("cross-venue-policy-inspect")
    commands.add_parser("cross-venue-fixture-acceptance")
    cross_materialize = commands.add_parser("cross-venue-allocation-materialize")
    cross_materialize.add_argument("--decision-id", required=True)
    cross_materialize.add_argument("--enriched-set-id", required=True)
    cross_materialize.add_argument("--decided-at", required=True)
    cross_materialize.add_argument("--idempotency-key", required=True)
    cross_decision = commands.add_parser("cross-venue-allocation-inspect")
    cross_decision.add_argument("--decision-id", required=True)
    cross_explanation = commands.add_parser("cross-venue-constraint-inspect")
    cross_explanation.add_argument("--decision-id", required=True)
    cross_explanation.add_argument("--projection-id")
    cross_exposure = commands.add_parser("cross-venue-exposure-inspect")
    cross_exposure.add_argument("--decision-id", required=True)
    cross_proposal = commands.add_parser("cross-venue-paper-trial-proposal-inspect")
    cross_proposal.add_argument("--proposal-id", required=True)
    cross_lineage = commands.add_parser("cross-venue-lineage-inspect")
    cross_lineage.add_argument("--decision-id", required=True)
    commands.add_parser("performance-attribution-fixture-acceptance")
    performance_materialize = commands.add_parser("performance-attribution-materialize")
    performance_materialize.add_argument("--run-id", required=True)
    performance_materialize.add_argument("--source-id", required=True)
    performance_materialize.add_argument("--materialized-at", required=True)
    performance_materialize.add_argument("--idempotency-key", required=True)
    performance_acceptance = commands.add_parser("performance-attribution-genuine-acceptance")
    performance_acceptance.add_argument("--run-id", required=True)
    performance_acceptance.add_argument("--as-of", required=True)
    performance_acceptance.add_argument("--idempotency-key", required=True)
    for command_name in (
        "performance-attribution-inspect",
        "performance-expected-realized-inspect",
        "performance-source-lineage-inspect",
    ):
        command = commands.add_parser(command_name)
        command.add_argument("--attribution-id", required=True)
    health_materialize = commands.add_parser("strategy-health-materialize")
    health_materialize.add_argument("--run-id", required=True)
    health_materialize.add_argument("--strategy-or-method-id", required=True)
    health_materialize.add_argument("--as-of", required=True)
    health_materialize.add_argument("--idempotency-key", required=True)
    health_inspect = commands.add_parser("strategy-health-inspect")
    health_inspect.add_argument("--health-id", required=True)
    venue_summary = commands.add_parser("performance-venue-summary-inspect")
    venue_summary.add_argument("--venue", required=True)
    commands.add_parser("performance-portfolio-summary-inspect")
    operating_register = commands.add_parser("operating-plan-register")
    operating_register.add_argument("--plan-id", required=True)
    operating_register.add_argument("--crypto-pulse-plan-id", required=True)
    operating_register.add_argument("--actor", required=True)
    operating_register.add_argument("--created-at", required=True)
    operating_register.add_argument("--idempotency-key", required=True)
    operating_plan = commands.add_parser("operating-plan-inspect")
    operating_plan.add_argument("--plan-id", required=True)
    operating_due = commands.add_parser("operating-due-plan")
    operating_due.add_argument("--cycle-id", required=True)
    operating_due.add_argument("--plan-id", required=True)
    operating_due.add_argument("--as-of", required=True)
    for command_name in ("operating-cycle-execute", "operating-cycle-replay"):
        operating_cycle = commands.add_parser(command_name)
        operating_cycle.add_argument("--cycle-id", required=True)
        operating_cycle.add_argument("--plan-id", required=True)
        operating_cycle.add_argument("--as-of", required=True)
        operating_cycle.add_argument("--executed-at", required=True)
        operating_cycle.add_argument("--idempotency-key", required=True)
    operating_cycle_inspect = commands.add_parser("operating-cycle-inspect")
    operating_cycle_inspect.add_argument("--cycle-id", required=True)
    operating_task = commands.add_parser("operating-task-inspect")
    operating_task.add_argument("--task-result-id", required=True)
    for command_name in ("operating-cycle-pause", "operating-cycle-resume"):
        operating_control = commands.add_parser(command_name)
        operating_control.add_argument("--event-id", required=True)
        operating_control.add_argument("--plan-id", required=True)
        operating_control.add_argument("--reason", required=True)
        operating_control.add_argument("--actor", required=True)
        operating_control.add_argument("--occurred-at", required=True)
        operating_control.add_argument("--idempotency-key", required=True)
    for command_name in ("operating-paper-activate", "operating-paper-deactivate"):
        operating_paper_activation = commands.add_parser(command_name)
        operating_paper_activation.add_argument("--event-id", required=True)
        operating_paper_activation.add_argument("--plan-id", required=True)
        operating_paper_activation.add_argument("--reason", required=True)
        operating_paper_activation.add_argument("--actor", required=True)
        operating_paper_activation.add_argument("--occurred-at", required=True)
        operating_paper_activation.add_argument("--idempotency-key", required=True)
    operating_paper_activation_inspect = commands.add_parser("operating-paper-activation-inspect")
    operating_paper_activation_inspect.add_argument("--plan-id", required=True)
    operating_brief = commands.add_parser("operating-brief-inspect")
    operating_brief.add_argument("--brief-id", required=True)
    operating_health = commands.add_parser("operating-health-inspect")
    operating_health.add_argument("--health-id", required=True)
    operating_checkpoint = commands.add_parser("operating-daily-checkpoint-inspect")
    operating_checkpoint.add_argument("--plan-id", required=True)
    operating_checkpoint.add_argument("--cycle-id", required=True)
    operating_checkpoint.add_argument("--runner-id", default="apex-operating-runner-v1")
    operating_worker = commands.add_parser("operating-cycle-worker")
    operating_worker.add_argument("--plan-id", required=True)
    operating_worker.add_argument("--runner-id", required=True)
    operating_worker.add_argument("--run-instance-id", required=True)
    operating_worker.add_argument("--once", action="store_true")
    operating_worker.add_argument("--poll-seconds", type=int, default=150)
    operating_worker.add_argument("--max-cycles", type=int, default=3)
    live_observer_worker = commands.add_parser("live-observer-worker")
    live_observer_worker.add_argument("--plan-id", required=True)
    live_observer_worker.add_argument("--runner-id", required=True)
    live_observer_worker.add_argument("--run-instance-id", required=True)
    live_observer_worker.add_argument("--once", action="store_true")
    live_observer_worker.add_argument("--poll-seconds", type=int, default=60)
    live_observer_worker.add_argument("--max-cycles", type=int, default=5)
    kalshi_capability = commands.add_parser("kalshi-operating-capability-inspect")
    kalshi_capability.add_argument("--capability-id", required=True)
    kalshi_capability.add_argument("--inspected-at", required=True)
    kalshi_capability.add_argument("--idempotency-key", required=True)
    for command_name in ("kalshi-operating-refresh", "kalshi-operating-refresh-replay"):
        kalshi_refresh = commands.add_parser(command_name)
        kalshi_refresh.add_argument("--refresh-id", required=True)
        kalshi_refresh.add_argument("--as-of", required=True)
        kalshi_refresh.add_argument("--run-key", required=True)
        kalshi_refresh.add_argument("--market-limit", type=int, default=10)
        kalshi_refresh.add_argument("--max-pages", type=int, default=1)
        kalshi_refresh.add_argument("--depth", type=int, default=5)
        kalshi_refresh.add_argument("--timeout-seconds", type=int, default=10)
        kalshi_refresh.add_argument("--idempotency-key", required=True)
    kalshi_refresh_read = commands.add_parser("kalshi-operating-refresh-inspect")
    kalshi_refresh_read.add_argument("--refresh-id", required=True)
    for command_name, argument_name in (
        ("kalshi-operating-market-inspect", "--market-snapshot-id"),
        ("kalshi-operating-rule-inspect", "--market-snapshot-id"),
        ("kalshi-operating-orderbook-inspect", "--orderbook-snapshot-id"),
    ):
        kalshi_evidence_read = commands.add_parser(command_name)
        kalshi_evidence_read.add_argument(argument_name, required=True)
    kalshi_opportunity_read = commands.add_parser("kalshi-operating-opportunity-inspect")
    kalshi_opportunity_read.add_argument("--refresh-id", required=True)
    kalshi_opportunity_read.add_argument("--opportunity-id", required=True)
    kalshi_account_create = commands.add_parser("kalshi-paper-account-create")
    kalshi_account_create.add_argument("--account-id", required=True)
    kalshi_account_create.add_argument("--opening-cash", required=True)
    kalshi_account_create.add_argument("--created-at", required=True)
    kalshi_account_create.add_argument("--idempotency-key", required=True)
    kalshi_account_read = commands.add_parser("kalshi-paper-account-inspect")
    kalshi_account_read.add_argument("--account-id", required=True)
    kalshi_scan = commands.add_parser("kalshi-paper-eligibility-scan")
    kalshi_scan.add_argument("--scan-id", required=True)
    kalshi_scan.add_argument("--refresh-id", required=True)
    kalshi_scan.add_argument("--shared-set-id", required=True)
    kalshi_scan.add_argument("--operating-plan-id", required=True)
    kalshi_scan.add_argument("--account-id")
    kalshi_scan.add_argument("--evaluated-at", required=True)
    kalshi_scan.add_argument("--idempotency-key", required=True)
    kalshi_authorize = commands.add_parser("kalshi-paper-authorization-create")
    kalshi_authorize.add_argument("--account-id", required=True)
    kalshi_authorize.add_argument("--refresh-id", required=True)
    kalshi_authorize.add_argument("--opportunity-id", required=True)
    kalshi_authorize.add_argument("--scan-id", required=True)
    kalshi_authorize.add_argument("--decision-id", required=True)
    kalshi_authorize.add_argument("--authorization-id", required=True)
    kalshi_authorize.add_argument("--maximum-contracts", type=int, required=True)
    kalshi_authorize.add_argument("--maximum-dollar-risk", required=True)
    kalshi_authorize.add_argument("--expires-at", required=True)
    kalshi_authorize.add_argument("--actor", required=True)
    kalshi_authorize.add_argument("--reason", required=True)
    kalshi_authorize.add_argument("--created-at", required=True)
    kalshi_authorize.add_argument("--idempotency-key", required=True)
    kalshi_preview = commands.add_parser("kalshi-paper-preview")
    kalshi_preview.add_argument("--account-id", required=True)
    kalshi_preview.add_argument("--refresh-id", required=True)
    kalshi_preview.add_argument("--opportunity-id", required=True)
    kalshi_preview.add_argument("--authorization-id", required=True)
    kalshi_preview.add_argument("--preview-id", required=True)
    kalshi_preview.add_argument("--limit-price", required=True)
    kalshi_preview.add_argument("--contracts", type=int, required=True)
    kalshi_preview.add_argument("--available-liquidity", type=int, required=True)
    kalshi_preview.add_argument("--evidence-id", required=True)
    kalshi_preview.add_argument("--evidence-observed-at", required=True)
    kalshi_preview.add_argument("--created-at", required=True)
    kalshi_preview.add_argument("--idempotency-key", required=True)
    kalshi_submit = commands.add_parser("kalshi-paper-submit")
    kalshi_submit.add_argument("--account-id", required=True)
    kalshi_submit.add_argument("--order-id", required=True)
    kalshi_submit.add_argument("--preview-id", required=True)
    kalshi_submit.add_argument("--submitted-at", required=True)
    kalshi_submit.add_argument("--idempotency-key", required=True)
    kalshi_fill = commands.add_parser("kalshi-paper-apply-fill")
    kalshi_fill.add_argument("--account-id", required=True)
    kalshi_fill.add_argument("--order-id", required=True)
    kalshi_fill.add_argument("--fill-id", required=True)
    kalshi_fill.add_argument("--contracts", type=int, required=True)
    kalshi_fill.add_argument("--price", required=True)
    kalshi_fill.add_argument("--evidence-id", required=True)
    kalshi_fill.add_argument("--evidence-observed-at", required=True)
    kalshi_fill.add_argument("--ingested-at", required=True)
    kalshi_fill.add_argument("--idempotency-key", required=True)
    kalshi_cancel = commands.add_parser("kalshi-paper-cancel")
    kalshi_cancel.add_argument("--account-id", required=True)
    kalshi_cancel.add_argument("--order-id", required=True)
    kalshi_cancel.add_argument("--canceled-at", required=True)
    kalshi_cancel.add_argument("--idempotency-key", required=True)
    kalshi_expire = commands.add_parser("kalshi-paper-expire")
    kalshi_expire.add_argument("--account-id", required=True)
    kalshi_expire.add_argument("--order-id", required=True)
    kalshi_expire.add_argument("--expired-at", required=True)
    kalshi_expire.add_argument("--idempotency-key", required=True)
    kalshi_settle = commands.add_parser("kalshi-paper-settle")
    kalshi_settle.add_argument("--account-id", required=True)
    kalshi_settle.add_argument("--position-id", required=True)
    kalshi_settle.add_argument("--settlement-id", required=True)
    kalshi_settle.add_argument("--resolution-fact-id", required=True)
    kalshi_settle.add_argument("--market-ticker", required=True)
    kalshi_settle.add_argument("--event-ticker", required=True)
    kalshi_settle.add_argument("--resolution", choices=("YES", "NO"), required=True)
    kalshi_settle.add_argument("--resolution-source", required=True)
    kalshi_settle.add_argument("--rule-hash", required=True)
    kalshi_settle.add_argument("--settled-at", required=True)
    kalshi_settle.add_argument("--retained-at", required=True)
    kalshi_settle.add_argument(
        "--evidence-class", choices=("FIXTURE", "GENUINE_PUBLIC"), required=True
    )
    kalshi_settle.add_argument("--idempotency-key", required=True)
    kalshi_reconcile = commands.add_parser("kalshi-paper-reconcile")
    kalshi_reconcile.add_argument("--account-id", required=True)
    kalshi_reconcile.add_argument("--reconciliation-id", required=True)
    kalshi_reconcile.add_argument("--reconciled-at", required=True)
    kalshi_reconcile.add_argument("--idempotency-key", required=True)
    for command_name, argument_name in (
        ("kalshi-paper-authorization-inspect", "--authorization-id"),
        ("kalshi-paper-order-inspect", "--order-id"),
        ("kalshi-paper-fill-inspect", "--fill-id"),
        ("kalshi-paper-position-inspect", "--position-id"),
        ("kalshi-paper-reconciliation-inspect", "--reconciliation-id"),
    ):
        kalshi_entity = commands.add_parser(command_name)
        kalshi_entity.add_argument("--account-id", required=True)
        kalshi_entity.add_argument(argument_name, required=True)
    commands.add_parser("kalshi-paper-fixture-acceptance")
    crypto_paper_contract = commands.add_parser("crypto-paper-contract-register")
    crypto_paper_contract.add_argument("--contract-id", required=True)
    crypto_paper_contract.add_argument("--capture-id", required=True)
    crypto_paper_contract.add_argument("--product-id", required=True)
    crypto_paper_contract.add_argument("--idempotency-key", required=True)
    crypto_paper_capability = commands.add_parser("crypto-paper-capability-inspect")
    crypto_paper_capability.add_argument("--capability-id", required=True)
    crypto_paper_capability.add_argument("--contract-id", required=True)
    crypto_paper_capability.add_argument("--inspected-at", required=True)
    crypto_paper_capability.add_argument("--idempotency-key", required=True)
    crypto_paper_account_create = commands.add_parser("crypto-paper-account-create")
    crypto_paper_account_create.add_argument("--account-id", required=True)
    crypto_paper_account_create.add_argument("--opening-cash", required=True)
    crypto_paper_account_create.add_argument("--created-at", required=True)
    crypto_paper_account_create.add_argument("--idempotency-key", required=True)
    for command_name in (
        "crypto-paper-account-inspect",
        "crypto-paper-open-orders",
        "crypto-paper-operating-seam-inspect",
    ):
        crypto_paper_account_read = commands.add_parser(command_name)
        crypto_paper_account_read.add_argument("--account-id", required=True)
    crypto_paper_scan = commands.add_parser("crypto-paper-genuine-eligibility-scan")
    crypto_paper_scan.add_argument("--scan-id", required=True)
    crypto_paper_scan.add_argument("--opportunity-set-id", required=True)
    crypto_paper_scan.add_argument("--universe-id", required=True)
    crypto_paper_scan.add_argument("--capture-id", required=True)
    crypto_paper_scan.add_argument("--operating-plan-id", required=True)
    crypto_paper_scan.add_argument("--evaluated-at", required=True)
    crypto_paper_scan.add_argument("--idempotency-key", required=True)
    crypto_paper_authorize = commands.add_parser("crypto-paper-authorization-create")
    crypto_paper_authorize.add_argument("--account-id", required=True)
    crypto_paper_authorize.add_argument("--authorization-id", required=True)
    crypto_paper_authorize.add_argument("--scan-id", required=True)
    crypto_paper_authorize.add_argument("--decision-id", required=True)
    crypto_paper_authorize.add_argument("--projection-id", required=True)
    crypto_paper_authorize.add_argument("--maximum-quote-notional", required=True)
    crypto_paper_authorize.add_argument("--maximum-base-size")
    crypto_paper_authorize.add_argument("--allowed-side", choices=("BUY", "SELL"), required=True)
    crypto_paper_authorize.add_argument(
        "--allowed-order-type",
        choices=("MARKET", "LIMIT"),
        required=True,
    )
    crypto_paper_authorize.add_argument("--expires-at", required=True)
    crypto_paper_authorize.add_argument("--actor", required=True)
    crypto_paper_authorize.add_argument("--reason", required=True)
    crypto_paper_authorize.add_argument("--created-at", required=True)
    crypto_paper_authorize.add_argument("--idempotency-key", required=True)
    crypto_paper_authorization_read = commands.add_parser("crypto-paper-authorization-inspect")
    crypto_paper_authorization_read.add_argument("--account-id", required=True)
    crypto_paper_authorization_read.add_argument("--authorization-id", required=True)
    crypto_paper_intent = commands.add_parser("crypto-paper-intent-construct")
    crypto_paper_intent.add_argument("--account-id", required=True)
    crypto_paper_intent.add_argument("--intent-id", required=True)
    crypto_paper_intent.add_argument("--authorization-id", required=True)
    crypto_paper_intent.add_argument("--contract-id", required=True)
    crypto_paper_intent.add_argument("--side", choices=("BUY", "SELL"), required=True)
    crypto_paper_intent.add_argument(
        "--order-type",
        choices=("MARKET", "LIMIT"),
        required=True,
    )
    crypto_paper_intent.add_argument("--quote-size")
    crypto_paper_intent.add_argument("--base-size")
    crypto_paper_intent.add_argument("--limit-price")
    crypto_paper_intent.add_argument("--created-at", required=True)
    crypto_paper_intent.add_argument("--idempotency-key", required=True)
    crypto_paper_preview = commands.add_parser("crypto-paper-preview")
    crypto_paper_preview.add_argument("--account-id", required=True)
    crypto_paper_preview.add_argument("--preview-id", required=True)
    crypto_paper_preview.add_argument("--intent-id", required=True)
    crypto_paper_preview.add_argument("--evidence-id", required=True)
    crypto_paper_preview.add_argument("--product-id", required=True)
    crypto_paper_preview.add_argument("--observed-at", required=True)
    crypto_paper_preview.add_argument("--ingested-at", required=True)
    crypto_paper_preview.add_argument("--price", required=True)
    crypto_paper_preview.add_argument("--available-base-quantity")
    crypto_paper_preview.add_argument(
        "--pricing-source",
        choices=(
            "GENUINE_TOP_OF_BOOK",
            "RETAINED_DAILY_OPEN_FALLBACK",
            "FIXTURE_RETAINED_OPEN",
        ),
        required=True,
    )
    crypto_paper_preview.add_argument("--evidence-class", required=True)
    crypto_paper_preview.add_argument("--fee-bps", required=True)
    crypto_paper_preview.add_argument("--spread-bps", required=True)
    crypto_paper_preview.add_argument("--slippage-bps", required=True)
    crypto_paper_preview.add_argument("--created-at", required=True)
    crypto_paper_preview.add_argument("--idempotency-key", required=True)
    crypto_paper_submit = commands.add_parser("crypto-paper-submit")
    crypto_paper_submit.add_argument("--account-id", required=True)
    crypto_paper_submit.add_argument("--order-id", required=True)
    crypto_paper_submit.add_argument("--reservation-id", required=True)
    crypto_paper_submit.add_argument("--preview-id", required=True)
    crypto_paper_submit.add_argument("--submitted-at", required=True)
    crypto_paper_submit.add_argument("--valid-until", required=True)
    crypto_paper_submit.add_argument("--actor", required=True)
    crypto_paper_submit.add_argument("--idempotency-key", required=True)
    for command_name in (
        "crypto-paper-order-inspect",
        "crypto-paper-fill-inspect",
        "crypto-paper-position-inspect",
        "crypto-paper-reconciliation-inspect",
    ):
        crypto_paper_entity = commands.add_parser(command_name)
        crypto_paper_entity.add_argument("--account-id", required=True)
        crypto_paper_entity.add_argument(
            {
                "crypto-paper-order-inspect": "--order-id",
                "crypto-paper-fill-inspect": "--fill-id",
                "crypto-paper-position-inspect": "--position-id",
                "crypto-paper-reconciliation-inspect": "--reconciliation-id",
            }[command_name],
            required=True,
        )
    crypto_paper_evidence = commands.add_parser("crypto-paper-apply-evidence")
    crypto_paper_evidence.add_argument("--account-id", required=True)
    crypto_paper_evidence.add_argument("--order-id", required=True)
    crypto_paper_evidence.add_argument("--fill-id", required=True)
    crypto_paper_evidence.add_argument("--evidence-id", required=True)
    crypto_paper_evidence.add_argument("--product-id", required=True)
    crypto_paper_evidence.add_argument("--observed-at", required=True)
    crypto_paper_evidence.add_argument("--ingested-at", required=True)
    crypto_paper_evidence.add_argument("--price", required=True)
    crypto_paper_evidence.add_argument("--available-base-quantity")
    crypto_paper_evidence.add_argument(
        "--pricing-source",
        choices=(
            "GENUINE_TOP_OF_BOOK",
            "RETAINED_DAILY_OPEN_FALLBACK",
            "FIXTURE_RETAINED_OPEN",
        ),
        required=True,
    )
    crypto_paper_evidence.add_argument("--evidence-class", required=True)
    crypto_paper_evidence.add_argument("--fee-bps", required=True)
    crypto_paper_evidence.add_argument("--slippage-bps", required=True)
    crypto_paper_evidence.add_argument("--actor", required=True)
    crypto_paper_evidence.add_argument("--idempotency-key", required=True)
    crypto_paper_cancel = commands.add_parser("crypto-paper-cancel")
    crypto_paper_cancel.add_argument("--account-id", required=True)
    crypto_paper_cancel.add_argument("--order-id", required=True)
    crypto_paper_cancel.add_argument("--canceled-at", required=True)
    crypto_paper_cancel.add_argument("--actor", required=True)
    crypto_paper_cancel.add_argument("--reason", required=True)
    crypto_paper_cancel.add_argument("--idempotency-key", required=True)
    crypto_paper_expire = commands.add_parser("crypto-paper-expire")
    crypto_paper_expire.add_argument("--account-id", required=True)
    crypto_paper_expire.add_argument("--order-id", required=True)
    crypto_paper_expire.add_argument("--expired-at", required=True)
    crypto_paper_expire.add_argument("--actor", required=True)
    crypto_paper_expire.add_argument("--idempotency-key", required=True)
    crypto_paper_reconcile = commands.add_parser("crypto-paper-reconcile")
    crypto_paper_reconcile.add_argument("--account-id", required=True)
    crypto_paper_reconcile.add_argument("--reconciliation-id", required=True)
    crypto_paper_reconcile.add_argument("--order-id")
    crypto_paper_reconcile.add_argument("--evaluated-at", required=True)
    crypto_paper_reconcile.add_argument("--idempotency-key", required=True)
    crypto_paper_ledger = commands.add_parser("crypto-paper-ledger-inspect")
    crypto_paper_ledger.add_argument("--account-id", required=True)
    crypto_paper_correct = commands.add_parser("crypto-paper-close-accounting-correct")
    crypto_paper_correct.add_argument("--account-id", required=True)
    crypto_paper_correct.add_argument("--sell-fill-id", required=True)
    crypto_paper_correct.add_argument("--expected-source-journal-hash", required=True)
    crypto_paper_correct.add_argument("--expected-restatement", required=True)
    crypto_paper_correct.add_argument("--idempotency-key", required=True)
    crypto_paper_correct.add_argument(
        "--apply",
        action="store_true",
        help="Append the correction; without it the command is a read-only dry run.",
    )
    commands.add_parser("crypto-paper-fixture-acceptance")
    commands.add_parser("autopilot-strategies")
    autopilot_status = commands.add_parser("autopilot-status")
    autopilot_status.add_argument("--campaign-id", required=True)
    autopilot_cycle = commands.add_parser("autopilot-cycle")
    autopilot_cycle.add_argument("--campaign-id", required=True)
    autopilot_cycle.add_argument("--mode", required=True, choices=("shadow", "paper"))
    autopilot_cycle.add_argument("--idempotency-key", required=True)
    autopilot_cycle.add_argument("--maximum-strategies", type=int, default=25)
    autopilot_cycle.add_argument("--maximum-proposals", type=int, default=100)
    autopilot_cycle.add_argument("--deadline-seconds", type=int, default=30)
    autopilot_cycle.add_argument("--operator-note", default="")
    for control_name in ("autopilot-pause", "autopilot-resume", "autopilot-kill"):
        control = commands.add_parser(control_name)
        control.add_argument("--campaign-id", required=True)
        control.add_argument("--reason", required=True)
        control.add_argument("--idempotency-key", required=True)
    commands.add_parser("autopilot-core-demo")
    commands.add_parser("paper-portfolio-demo")
    commands.add_parser("strategy-shadow-demo")
    commands.add_parser("orderbook-microstructure-shadow-demo")
    commands.add_parser("market-dynamics-shadow-demo")
    multi_cycle = commands.add_parser("multi-strategy-shadow-cycle")
    multi_cycle.add_argument("--campaign-id", required=True)
    multi_cycle.add_argument("--idempotency-key", required=True)
    multi_cycle.add_argument("--market-limit", type=int, default=20)
    multi_cycle.add_argument("--maximum-predictions", type=int, default=100)
    multi_cycle.add_argument("--maximum-opportunities", type=int, default=40)
    multi_session = commands.add_parser("multi-strategy-shadow-session")
    multi_session.add_argument("--campaign-id", required=True)
    multi_session.add_argument("--iterations", type=int, required=True)
    multi_session.add_argument("--interval-seconds", type=int, required=True)
    multi_session.add_argument("--market-limit", type=int, required=True)
    multi_session.add_argument("--max-pages", type=int, required=True)
    multi_session.add_argument("--max-enrichments", type=int, required=True)
    multi_session.add_argument("--orderbook-depth", type=int, required=True)
    multi_reconcile = commands.add_parser("reconcile-multi-strategy-shadow-session")
    multi_reconcile.add_argument("--session-id", required=True)
    commands.add_parser("multi-strategy-shadow-demo")
    create_plan = commands.add_parser("create-shadow-plan")
    create_plan.add_argument("--campaign-id", required=True)
    create_plan.add_argument("--name", required=True)
    create_plan.add_argument("--iterations", type=int, default=12)
    create_plan.add_argument("--interval-seconds", type=int, default=300)
    create_plan.add_argument("--market-count", type=int, default=10)
    create_plan.add_argument("--max-pages", type=int, default=3)
    create_plan.add_argument("--max-enrichments", type=int, default=30)
    create_plan.add_argument("--max-markets-per-event", type=int, default=2)
    create_plan.add_argument("--max-markets-per-series", type=int, default=4)
    create_plan.add_argument("--orderbook-depth", type=int, default=5)
    create_plan.add_argument(
        "--market-tickers",
        help="optional comma-separated exact retained market tickers (three to fifty)",
    )
    create_plan.add_argument("--idempotency-key", required=True)
    atomic_plan = commands.add_parser("discover-create-shadow-plan")
    atomic_plan.add_argument("--campaign-id", required=True)
    atomic_plan.add_argument("--name", required=True)
    atomic_plan.add_argument("--window-start", required=True, type=_review_timestamp)
    atomic_plan.add_argument("--window-end", required=True, type=_review_timestamp)
    atomic_plan.add_argument("--iterations", required=True, type=int)
    atomic_plan.add_argument("--interval-seconds", required=True, type=int)
    atomic_plan.add_argument("--target-markets", required=True, type=int)
    atomic_plan.add_argument("--minimum-markets", required=True, type=int)
    atomic_plan.add_argument("--minimum-events", required=True, type=int)
    atomic_plan.add_argument("--max-pages", required=True, type=int)
    atomic_plan.add_argument("--max-inspected-markets", required=True, type=int)
    atomic_plan.add_argument("--max-enrichments", required=True, type=int)
    atomic_plan.add_argument("--max-markets-per-event", required=True, type=int)
    atomic_plan.add_argument("--max-markets-per-series", required=True, type=int)
    atomic_plan.add_argument("--orderbook-depth", required=True, type=int)
    atomic_plan.add_argument("--max-orderbook-preflights", type=int, default=80)
    atomic_plan.add_argument(
        "--microstructure-configuration-version",
        default="orderbook-microstructure-shadow-v1",
    )
    atomic_plan.add_argument("--idempotency-key", required=True)
    atomic_plan.add_argument("--exclude-plan-id", action="append", default=[])
    atomic_plan.add_argument(
        "--exclude-prior-plan-markets", action=argparse.BooleanOptionalAction, default=True
    )
    atomic_plan.add_argument("--exclude-prior-plan-events", action="store_true")
    atomic_plan.add_argument(
        "--exclude-active-plan-markets", action=argparse.BooleanOptionalAction, default=True
    )
    run_plan = commands.add_parser("run-shadow-plan")
    run_plan.add_argument("--plan-id", required=True)
    run_plan.add_argument("--once", action="store_true")
    worker = commands.add_parser("shadow-worker")
    worker.add_argument("--plan-id")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-seconds", type=int, default=5)
    worker.add_argument("--maximum-plans", type=int, default=1)
    outcomes = commands.add_parser("collect-shadow-outcomes")
    outcomes.add_argument("--campaign-id", required=True)
    outcomes.add_argument(
        "--market-limit", "--maximum-markets", dest="market_limit", type=int, default=100
    )
    performance = commands.add_parser("strategy-performance")
    performance.add_argument("--campaign-id", required=True)
    performance.add_argument("--strategy-id")
    performance.add_argument("--plan-id")
    evidence_snapshot = commands.add_parser("strategy-evidence-snapshot")
    evidence_snapshot.add_argument("--strategy-id", required=True)
    evidence_snapshot.add_argument("--configuration-hash", required=True)
    evidence_snapshot.add_argument("--policy-id", required=True)
    evidence_snapshot.add_argument("--cutoff", required=True)
    review_request = commands.add_parser("request-strategy-review")
    review_request.add_argument("--strategy-id", required=True)
    review_request.add_argument("--campaign-id", required=True)
    review_request.add_argument("--snapshot-id", required=True)
    review_request.add_argument("--reason", required=True)
    review_request.add_argument("--idempotency-key", required=True)
    review_status = commands.add_parser("strategy-review-status")
    review_status.add_argument("--review-id", required=True)
    paper_approval = commands.add_parser("approve-paper-trial")
    paper_approval.add_argument("--review-id", required=True)
    paper_approval.add_argument("--campaign-id", required=True)
    paper_approval.add_argument("--starts-at", required=True)
    paper_approval.add_argument("--expires-at", required=True)
    paper_approval.add_argument("--maximum-cycles", type=int, required=True)
    paper_approval.add_argument("--maximum-orders", type=int, required=True)
    paper_approval.add_argument("--maximum-positions", type=int, required=True)
    paper_approval.add_argument("--maximum-loss", required=True)
    paper_approval.add_argument("--confirmation-strategy", required=True)
    paper_approval.add_argument("--confirmation-campaign", required=True)
    paper_approval.add_argument("--confirmation-snapshot", required=True)
    paper_approval.add_argument("--idempotency-key", required=True)
    commands.add_parser("evidence-review-paper-trial-demo")
    commands.add_parser("shadow-operations-demo")
    create_shadow_campaign = commands.add_parser("create-shadow-campaign")
    create_shadow_campaign.add_argument("--name", required=True)
    create_shadow_campaign.add_argument("--capital", required=True)
    create_shadow_campaign.add_argument("--maximum-loss", required=True)
    create_shadow_campaign.add_argument("--expires-in-days", type=int, required=True)
    create_shadow_campaign.add_argument("--idempotency-key", required=True)
    paper_status = commands.add_parser("paper-portfolio-status")
    paper_status.add_argument("--campaign-id", required=True)
    paper_mark = commands.add_parser("paper-mark-positions")
    paper_mark.add_argument("--campaign-id", required=True)
    paper_mark.add_argument("--orderbook-run-id", required=True)
    paper_settle = commands.add_parser("paper-settle-position")
    paper_settle.add_argument("--position-id", required=True)
    paper_settle.add_argument("--settlement-fact-id", required=True)
    authority = commands.add_parser("strategy-authority")
    authority.add_argument("--strategy-id", required=True)
    authority.add_argument("--requested-authority", required=True)
    authority.add_argument("--operator-reason", required=True)
    authority.add_argument("--evidence-review-id", required=True)
    authority.add_argument("--idempotency-key", required=True)
    authority.add_argument("--confirmation-text")
    shadow_session = commands.add_parser("live-shadow-session")
    shadow_session.add_argument("--campaign-id", required=True)
    shadow_session.add_argument("--strategy-id", required=True)
    shadow_session.add_argument("--iterations", type=int, required=True)
    shadow_session.add_argument("--interval-seconds", type=int, required=True)
    shadow_session.add_argument("--market-limit", type=int, required=True)
    shadow_session.add_argument("--maximum-pages", type=int, default=1)
    shadow_session.add_argument("--maximum-enrichments", type=int, default=20)
    shadow_session.add_argument("--observations-per-market", type=int, default=3)
    shadow_session.add_argument("--orderbook-depth", type=int, default=5)

    cmr_agenda_create = commands.add_parser("research-agenda-create")
    cmr_agenda_create.add_argument("--dataset-snapshot-id", required=True)
    cmr_agenda_create.add_argument("--replay-run-id", required=True)
    cmr_agenda_create.add_argument("--cross-market-dataset-id", required=True)
    cmr_agenda_create.add_argument("--cohort-policy-id", required=True)
    cmr_agenda_create.add_argument("--created-at", required=True)
    cmr_agenda_inspect = commands.add_parser("research-agenda-inspect")
    cmr_agenda_inspect.add_argument("--agenda-id", required=True)
    cmr_proposals_generate = commands.add_parser("hypothesis-proposals-generate")
    cmr_proposals_generate.add_argument("--agenda-id", required=True)
    cmr_proposals_generate.add_argument("--created-at", required=True)
    cmr_proposals_list = commands.add_parser("hypothesis-proposals-list")
    cmr_proposals_list.add_argument("--agenda-id", required=True)
    for command_name in ("agenda-run-start", "agenda-run-resume"):
        cmr_run_step = commands.add_parser(command_name)
        cmr_run_step.add_argument("--agenda-id", required=True)
        cmr_run_step.add_argument("--train-end", required=True)
        cmr_run_step.add_argument("--validation-end", required=True)
        cmr_run_step.add_argument("--maximum-hypotheses", type=int, default=25)
        cmr_run_step.add_argument("--created-at", required=True)
    cmr_run_inspect = commands.add_parser("agenda-run-inspect")
    cmr_run_inspect.add_argument("--agenda-id", required=True)
    cmr_knowledge_inspect = commands.add_parser("research-knowledge-inspect")
    cmr_knowledge_inspect.add_argument("--hypothesis-id", required=True)
    commands.add_parser("research-contradictions-list")
    commands.add_parser("follow-up-proposals-list")
    cmr_quality_inspect = commands.add_parser("research-agenda-quality-inspect")
    cmr_quality_inspect.add_argument("--agenda-id", required=True)

    rc_create = commands.add_parser("research-calibration-create")
    rc_create.add_argument("--dataset-snapshot-id", required=True)
    rc_create.add_argument("--replay-run-id", required=True)
    rc_create.add_argument("--cross-market-dataset-id", required=True)
    rc_create.add_argument("--source-agenda-id", required=True)
    rc_create.add_argument("--created-at", required=True)
    rc_inspect = commands.add_parser("research-calibration-inspect")
    rc_inspect.add_argument("--calibration-id", required=True)
    for command_name in ("research-calibration-run-start", "research-calibration-run-resume"):
        rc_run_step = commands.add_parser(command_name)
        rc_run_step.add_argument("--calibration-id", required=True)
        rc_run_step.add_argument("--train-end", required=True)
        rc_run_step.add_argument("--validation-end", required=True)
        rc_run_step.add_argument("--maximum-specs", type=int, default=104)
        rc_run_step.add_argument("--created-at", required=True)
    rc_run_inspect = commands.add_parser("research-calibration-run-inspect")
    rc_run_inspect.add_argument("--calibration-id", required=True)
    rc_report_inspect = commands.add_parser("research-calibration-report-inspect")
    rc_report_inspect.add_argument("--calibration-id", required=True)
    rc_grade_inspect = commands.add_parser("research-evidence-grade-inspect")
    rc_grade_inspect.add_argument("--calibration-id", required=True)
    rc_grade_inspect.add_argument("--hypothesis-id", required=True)
    rc_agenda_recalibrate = commands.add_parser("research-agenda-recalibrate")
    rc_agenda_recalibrate.add_argument("--calibration-id", required=True)
    rc_agenda_recalibrate.add_argument("--created-at", required=True)
    rc_follow_up_list = commands.add_parser("follow-up-priority-list")
    rc_follow_up_list.add_argument("--calibration-id", required=True)

    rc2_create = commands.add_parser("calibration-v2-create")
    rc2_create.add_argument("--source-calibration-id", required=True)
    rc2_create.add_argument("--created-at", required=True)
    rc2_inspect = commands.add_parser("calibration-v2-inspect")
    rc2_inspect.add_argument("--calibration-id", required=True)
    for command_name in ("calibration-v2-run-start", "calibration-v2-run-resume"):
        rc2_run_step = commands.add_parser(command_name)
        rc2_run_step.add_argument("--calibration-id", required=True)
        rc2_run_step.add_argument("--source-calibration-id", required=True)
        rc2_run_step.add_argument("--train-end", required=True)
        rc2_run_step.add_argument("--validation-end", required=True)
        rc2_run_step.add_argument("--maximum-specs", type=int, default=20)
        rc2_run_step.add_argument("--created-at", required=True)
    rc2_run_inspect = commands.add_parser("calibration-v2-run-inspect")
    rc2_run_inspect.add_argument("--calibration-id", required=True)
    rc2_recalibrate = commands.add_parser("calibration-v2-recalibrate-agenda")
    rc2_recalibrate.add_argument("--calibration-id", required=True)
    rc2_recalibrate.add_argument("--source-calibration-id", required=True)
    rc2_recalibrate.add_argument("--created-at", required=True)
    rc2_strata = commands.add_parser("calibration-strata-inspect")
    rc2_strata.add_argument("--calibration-id", required=True)
    rc2_strata.add_argument("--research-design", required=False, default=None)
    rc2_stability = commands.add_parser("calibration-stability-inspect")
    rc2_stability.add_argument("--calibration-id", required=True)
    rc2_requalify = commands.add_parser("follow-up-requalify")
    rc2_requalify.add_argument("--calibration-id", required=True)
    rc2_requalify.add_argument("--source-calibration-id", required=True)
    rc2_requalify.add_argument("--created-at", required=True)
    rc2_grade = commands.add_parser("evidence-grade-v2-inspect")
    rc2_grade.add_argument("--calibration-id", required=True)
    rc2_grade.add_argument("--subject-id", required=True)

    rr_create = commands.add_parser("research-replication-create")
    rr_create.add_argument("--follow-up-id", required=True)
    rr_create.add_argument("--v2-calibration-id", required=True)
    rr_create.add_argument("--created-at", required=True)
    rr_inspect = commands.add_parser("research-replication-inspect")
    rr_inspect.add_argument("--replication-id", required=True)
    for command_name in ("research-replication-run", "research-replication-run-resume"):
        rr_run_step = commands.add_parser(command_name)
        rr_run_step.add_argument("--replication-id", required=True)
        rr_run_step.add_argument("--created-at", required=True)
    rr_stability = commands.add_parser("replication-stability-inspect")
    rr_stability.add_argument("--replication-id", required=True)
    rr_timing = commands.add_parser("replication-timing-inspect")
    rr_timing.add_argument("--replication-id", required=True)
    rr_conclusion = commands.add_parser("replication-conclusion-inspect")
    rr_conclusion.add_argument("--replication-id", required=True)

    # External Strategy Evaluation v1.
    ext_register = commands.add_parser("external-strategy-register")
    ext_register.add_argument("--spec-file", required=True)
    ext_register.add_argument("--registered-at", required=False, default=None)
    ext_evaluate = commands.add_parser("external-strategy-evaluate")
    ext_evaluate.add_argument("--strategy-id", required=True)
    ext_evaluate.add_argument("--version", required=False, default=None, type=int)
    ext_evaluate.add_argument("--capture-id", required=True)
    ext_evaluate.add_argument("--as-of", required=True)
    ext_evaluate.add_argument("--evaluated-at", required=False, default=None)
    ext_promote = commands.add_parser("external-strategy-promote")
    ext_promote.add_argument("--strategy-id", required=True)
    ext_promote.add_argument("--version", required=True, type=int)
    ext_promote.add_argument("--evaluation-id", required=True)
    ext_promote.add_argument("--promoted-at", required=False, default=None)
    ext_inspect = commands.add_parser("external-strategy-inspect")
    ext_inspect.add_argument("--strategy-id", required=True)
    commands.add_parser("external-strategy-list")

    # Strategy Evaluation History & Promotion Lineage v1: read-only
    # inspection of the LIVE recurring STRATEGY_PAPER_TRIAL_EXECUTION
    # catalog's own durably retained evaluation/promotion lineage, plus the
    # one explicit, operator-invoked promotion action -- deliberately
    # separate commands from external-strategy-* above (see
    # ``domain.strategy_evaluation_history``'s own module docstring for the
    # "internal" vocabulary caveat). There is no "-register" command: this
    # catalog's four candidates are a fixed, reviewed, code-defined set
    # (``domain.strategy_benchmark.canonical_strategy_benchmarks``), never
    # operator-registered.
    commands.add_parser("internal-strategy-list")
    int_inspect = commands.add_parser("internal-strategy-inspect")
    int_inspect.add_argument("--strategy-id", required=True)
    int_promote = commands.add_parser("internal-strategy-promote")
    int_promote.add_argument("--strategy-id", required=True)
    int_promote.add_argument("--evaluation-id", required=True)
    int_promote.add_argument("--promoted-at", required=False, default=None)
    int_promote.add_argument(
        "--actor", required=False, default="operator:cli:internal-strategy-promote"
    )
    # Strategy Version Provenance v1: read-only exact-artifact lineage.
    int_provenance = commands.add_parser("internal-strategy-provenance")
    int_provenance.add_argument("--strategy-id", required=True)
    # Strategy PAPER Trial Outcome Assessment v1: read-only outcome evidence.
    int_outcomes = commands.add_parser("internal-strategy-trial-outcomes")
    int_outcomes.add_argument("--strategy-id", required=False, default=None)
    int_outcomes.add_argument("--trial-id", required=False, default=None)
    # Historical Evidence Gap Recovery v1.
    gap_inspect = commands.add_parser("evidence-gap-inspect")
    gap_inspect.add_argument("--family", required=False, default=None)
    gap_inspect.add_argument("--limit", type=int, default=50)
    gap_run = commands.add_parser("evidence-gap-recovery-run")
    gap_run.add_argument(
        "--mode",
        choices=("preview", "detect", "recover"),
        default="preview",
        help="preview: read-only; detect: record irrecoverable gaps (no provider); "
        "recover: bounded provider history GETs",
    )
    gap_run.add_argument("--cycle-key", default=None)
    gap_path = commands.add_parser("evidence-gap-vintage-path-correct")
    gap_path.add_argument(
        "--apply", action="store_true", help="append corrections (default: dry run)"
    )
    gap_path.add_argument("--expected-count", type=int, default=None)

    return root


def _decision_inputs(fixture: DecisionFixture) -> DecisionInputs:
    baseline_contribution = Money(fixture.campaign.campaign_contribution)
    baseline_authorized = Money(fixture.campaign.authorized_campaign_loss)
    authorized_fraction = RiskFraction(fixture.campaign.authorized_loss_fraction)
    if baseline_authorized.amount != baseline_contribution.amount * authorized_fraction.value:
        raise ValueValidationError("fixture authorized loss and fraction are inconsistent")
    policy = CampaignPolicy(
        policy_version_id=fixture.policy.policy_version_id,
        profile=fixture.policy.profile,
        requested_risk_fraction=fixture.policy.requested_risk_fraction,
        max_trade_loss_fraction=fixture.policy.max_trade_loss_fraction,
        max_open_loss_fraction=fixture.policy.max_open_loss_fraction,
        min_synthetic_edge=fixture.policy.min_synthetic_edge,
        max_spread=fixture.policy.max_spread,
        max_capital_lockup_seconds=fixture.policy.max_capital_lockup_seconds,
        content_hash=ContentHash(fixture.policy.content_hash),
    )
    instrument_fixture = fixture.instrument
    instrument = Instrument(
        instrument_id=instrument_fixture.instrument_id,
        category=instrument_fixture.category,
        payout_unit=parse_decimal(instrument_fixture.payout_unit),
        price_tick=parse_decimal(instrument_fixture.price_tick),
        quantity_increment=parse_decimal(instrument_fixture.quantity_increment),
        yes_settlement_value=parse_decimal(instrument_fixture.yes_settlement_value),
        no_settlement_value=parse_decimal(instrument_fixture.no_settlement_value),
        void_settlement_value=parse_decimal(instrument_fixture.void_settlement_value),
    )
    rule_fixture = fixture.contract_rule
    if isinstance(rule_fixture, VoidContractRuleFixture):
        term = rule_fixture.void_settlement_term
        rule: ContractRuleVersion = VoidContractRuleVersion(
            rule_version_id=rule_fixture.rule_version_id,
            exact_text=rule_fixture.exact_text,
            resolution_source=rule_fixture.resolution_source,
            effective_at=rule_fixture.effective_at,
            close_at=rule_fixture.close_at,
            determination_at=rule_fixture.determination_at,
            expected_settlement_at=rule_fixture.expected_settlement_at,
            content_hash=ContentHash(rule_fixture.content_hash),
            void_settlement_term=VoidSettlementRuleTerm(
                outcome_classification=SettlementOutcome(term.outcome_classification),
                settlement_value=parse_decimal(term.settlement_value),
                settlement_value_text=term.settlement_value,
                content_hash=ContentHash(term.content_hash),
            ),
        )
    else:
        rule = ContractRuleVersion(
            rule_version_id=rule_fixture.rule_version_id,
            exact_text=rule_fixture.exact_text,
            resolution_source=rule_fixture.resolution_source,
            effective_at=rule_fixture.effective_at,
            close_at=rule_fixture.close_at,
            determination_at=rule_fixture.determination_at,
            expected_settlement_at=rule_fixture.expected_settlement_at,
            content_hash=ContentHash(rule_fixture.content_hash),
        )
    source_records = tuple(
        SourceRecord(
            source_id=record.source_id,
            source_event_at=record.source_event_at,
            available_at=record.available_at,
            sequence=record.sequence,
            schema_version=record.schema_version,
            content_hash=ContentHash(record.content_hash),
        )
        for record in fixture.source_records
    )
    market = MarketSnapshot(
        yes_ask=Price(instrument_fixture.yes_ask, instrument_fixture.price_tick),
        no_ask=Price(instrument_fixture.no_ask, instrument_fixture.price_tick),
        visible_yes_quantity=Quantity(
            instrument_fixture.visible_yes_quantity,
            instrument_fixture.quantity_increment,
        ),
    )
    estimate_fixture = fixture.reference_estimate
    estimate = ReferenceEstimateInput(
        estimate_id=estimate_fixture.estimate_id,
        source_id=estimate_fixture.source_id,
        probability_vector=ProbabilityVector(
            estimate_fixture.probability_yes,
            estimate_fixture.probability_no,
            estimate_fixture.probability_void,
        ),
        published_at=estimate_fixture.published_at,
        expires_at=estimate_fixture.expires_at,
        method_version=estimate_fixture.method_version,
    )
    return DecisionInputs(
        fixture_id=fixture.fixture_id,
        schema_version=fixture.schema_version,
        decision_data_hash=ContentHash(fixture.decision_data_hash),
        cutoff=InformationCutoff(fixture.decision_cutoff),
        campaign_id=fixture.campaign.campaign_id,
        authorized_loss_fraction=fixture.campaign.authorized_loss_fraction,
        policy=policy,
        instrument=instrument,
        rule=rule,
        source_records=source_records,
        market=market,
        reference_estimate=estimate,
        strategy_version=fixture.strategy_version,
        fee_model_version=fixture.fee_model_version,
        strategy_decision_at=fixture.strategy_decision_at,
        fee_per_contract=fixture.fee_per_contract,
        resting_limit_price=(
            Price(fixture.resting_limit_price, instrument_fixture.price_tick)
            if fixture.resting_limit_price is not None
            else None
        ),
        order_expires_at=fixture.order_expires_at,
        risk_authorization_at=fixture.authorization_at,
        market_observation_source_id=fixture.market_observation_source_id,
    )


def _execution_script(fixture: DecisionFixture) -> ExecutionScript:
    script = fixture.execution
    if isinstance(script, PartialFillCanceledExecutionScriptFixture):
        return PartialFillCanceledExecutionScript(
            script_id=script.script_id,
            kind=script.kind,
            side=script.side,
            executable_yes_ask=script.executable_yes_ask,
            executable_no_ask=script.executable_no_ask,
            visible_yes_quantity=script.visible_yes_quantity,
            ordered_quantity=script.ordered_quantity,
            fill_quantity=script.fill_quantity,
            remaining_quantity=script.remaining_quantity,
            opened_at=script.opened_at,
            filled_at=script.filled_at,
            canceled_at=script.canceled_at,
            opened_sequence=script.opened_sequence,
            fill_sequence=script.fill_sequence,
            canceled_sequence=script.canceled_sequence,
            fill_price=script.fill_price,
            fee_per_contract=script.fee_per_contract,
        )
    if isinstance(script, UnfilledExpiryExecutionScriptFixture):
        return UnfilledExpiryExecutionScript(
            script_id=script.script_id,
            kind=script.kind,
            side=script.side,
            executable_yes_ask=script.executable_yes_ask,
            executable_no_ask=script.executable_no_ask,
            visible_yes_quantity=script.visible_yes_quantity,
            opened_at=script.opened_at,
            expires_at=script.expires_at,
            opened_sequence=script.opened_sequence,
            expired_sequence=script.expired_sequence,
        )
    return SyntheticExecutionScript(
        script_id=script.script_id,
        kind=script.kind,
        opened_at=script.opened_at,
        filled_at=script.filled_at,
        opened_sequence=script.opened_sequence,
        fill_sequence=script.fill_sequence,
        fill_price=script.fill_price,
        fee_per_contract=script.fee_per_contract,
    )


def _settlement_inputs(fixture: SettlementFixture) -> SettlementInputs:
    fact: SettlementFact
    if isinstance(fixture, ClassifiedRuleBoundSettlementFixture):
        fact = ClassifiedRuleBoundSettlementFact(
            source_id=fixture.source_id,
            instrument_id=fixture.instrument_id,
            rule_version_id=fixture.rule_version_id,
            source_event_at=fixture.source_event_at,
            available_at=fixture.available_at,
            settlement_value=parse_decimal(fixture.settlement_value),
            content_hash=ContentHash(fixture.content_hash),
            fixture_id=fixture.fixture_id,
            schema_version=fixture.schema_version,
            settlement_value_text=fixture.settlement_value,
            resolution_source=fixture.resolution_source,
            rule_content_hash=ContentHash(fixture.rule_content_hash),
            outcome_classification=SettlementOutcome(fixture.outcome_classification),
            raw_payload=fixture.raw_payload,
            raw_payload_hash=ContentHash(fixture.raw_payload_hash),
        )
    elif isinstance(fixture, RuleBoundSettlementFixture):
        fact = RuleBoundSettlementFact(
            source_id=fixture.source_id,
            instrument_id=fixture.instrument_id,
            rule_version_id=fixture.rule_version_id,
            source_event_at=fixture.source_event_at,
            available_at=fixture.available_at,
            settlement_value=parse_decimal(fixture.settlement_value),
            content_hash=ContentHash(fixture.content_hash),
            fixture_id=fixture.fixture_id,
            schema_version=fixture.schema_version,
            settlement_value_text=fixture.settlement_value,
            resolution_source=fixture.resolution_source,
            rule_content_hash=ContentHash(fixture.rule_content_hash),
            raw_payload=fixture.raw_payload,
            raw_payload_hash=ContentHash(fixture.raw_payload_hash),
        )
    else:
        fact = SettlementFact(
            source_id=fixture.source_id,
            instrument_id=fixture.instrument_id,
            rule_version_id=fixture.rule_version_id,
            source_event_at=fixture.source_event_at,
            available_at=fixture.available_at,
            settlement_value=parse_decimal(fixture.settlement_value),
            content_hash=ContentHash(fixture.raw_payload_hash),
        )
    return SettlementInputs(
        fixture_id=fixture.fixture_id,
        settlement_file_hash=ContentHash(fixture.file_hash),
        fact=fact,
    )


def demo_core(fixture_id: str, capital: Money, seed: int) -> bytes:
    reader = FixtureReader()
    decision_fixture = reader.load_decision(fixture_id)
    metadata = RunMetadata(
        fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
        decision_file_hash=ContentHash(decision_fixture.file_hash),
        logical_time=decision_fixture.logical_time,
    )
    simulator = DeterministicSimulator()
    result = run_simulation(
        decision_inputs=_decision_inputs(decision_fixture),
        execution_script=_execution_script(decision_fixture),
        run_metadata=metadata,
        settlement_loader=lambda: _settlement_inputs(reader.load_settlement(fixture_id)),
        capital=capital,
        seed=seed,
        engine_version=ENGINE_VERSION,
        simulator=simulator,
    )
    return CanonicalReportRenderer().render(report_document(result))


class _Disposable(Protocol):
    def dispose(self) -> None: ...


def _discovery_store(*, persist: bool) -> tuple[PublicMarketDiscoveryStore, _Disposable | None]:
    if not persist:
        return InMemoryPublicMarketDiscoveryStore(), None
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_public_market_discovery_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return postgres_public_market_discovery_store(engine), engine


def _print_public_market_summary(summary: PublicMarketScanSummary) -> None:
    print(
        " ".join(
            (
                f"run_id={summary.ingestion_run_id}",
                f"state={summary.state.value}",
                f"pages={summary.page_count}",
                f"markets={summary.market_count}",
                f"eligible={summary.eligible_count}",
                f"ineligible={summary.ineligible_count}",
                f"invalid={summary.invalid_count}",
                f"replay={str(summary.idempotent_replay).lower()}",
            )
        )
    )


def _print_public_event_consistency_summary(summary: PublicEventConsistencyScanSummary) -> None:
    print(
        " ".join(
            (
                f"run_id={summary.ingestion_run_id}",
                f"state={summary.state.value}",
                f"pages={summary.page_count}",
                f"events={summary.event_count}",
                f"consistent={summary.consistent_count}",
                f"candidates={summary.candidate_count}",
                f"ineligible={summary.ineligible_count}",
                f"invalid={summary.invalid_count}",
                f"ranked_events={','.join(summary.ranked_event_tickers) or '-'}",
                f"replay={str(summary.idempotent_replay).lower()}",
            )
        )
    )


def _print_scan_comparison_report(report: PublicScanComparisonReport) -> None:
    comparison = report.comparison
    counts = dict(comparison.candidate_transition_counts)
    observation_interval = (
        comparison.current_observed_start_at - comparison.baseline_observed_end_at
    )
    priorities = ",".join(item.provider_event_ticker for item in report.ranked_priorities) or "-"
    print(
        " ".join(
            (
                f"comparison_id={comparison.comparison_id}",
                f"baseline_run_id={comparison.baseline_ingestion_run_id}",
                f"current_run_id={comparison.current_ingestion_run_id}",
                f"observation_interval={observation_interval}",
                f"markets_matched={comparison.matched_market_count}",
                f"markets_added={comparison.added_market_count}",
                f"markets_removed={comparison.removed_market_count}",
                f"markets_changed={comparison.changed_market_count}",
                f"events_matched={comparison.matched_event_count}",
                f"new_candidates={counts.get('NEW_CANDIDATE', 0)}",
                f"persisting_candidates={counts.get('PERSISTING_CANDIDATE', 0)}",
                f"resolved_candidates={counts.get('RESOLVED_CANDIDATE', 0)}",
                f"became_ineligible={counts.get('BECAME_INELIGIBLE', 0)}",
                f"became_invalid={counts.get('BECAME_INVALID', 0)}",
                f"priorities={priorities}",
                f"replay={str(report.idempotent_replay).lower()}",
            )
        )
    )


def _print_event_history_report(report: EventHistoryReport) -> None:
    analysis = report.analysis
    counts = {
        "candidate": analysis.candidate_observation_count,
        "consistent": analysis.consistent_observation_count,
        "ineligible": analysis.ineligible_observation_count,
        "invalid": analysis.invalid_observation_count,
    }
    timeline = ",".join(
        f"{value.ordinal}:{value.state.value}:{value.continuity_state.value}:"
        f"{'+'.join(value.reason_codes) or '-'}"
        for value in report.observations
    )
    print(
        " ".join(
            (
                f"event_ticker={analysis.provider_event_ticker}",
                f"first_observed_at={report.observations[0].run_observed_start_at.isoformat()}",
                f"last_observed_at={report.observations[-1].run_observed_end_at.isoformat()}",
                f"included_runs={analysis.included_completed_run_count}",
                f"present={analysis.present_observation_count}",
                f"absent={analysis.absent_observation_count}",
                f"candidate={counts['candidate']}",
                f"consistent={counts['consistent']}",
                f"ineligible={counts['ineligible']}",
                f"invalid={counts['invalid']}",
                f"evidence_breaks={analysis.evidence_break_count}",
                f"maximum_observation_gap={analysis.maximum_observation_gap}",
                f"current_candidate_streak={analysis.current_candidate_streak_observation_count}",
                f"longest_candidate_streak={analysis.longest_candidate_streak_observation_count}",
                f"current_deviation={analysis.current_consistency_deviation}",
                f"maximum_compatible_deviation={analysis.maximum_compatible_consistency_deviation}",
                f"minimum_visible_size_current_streak={analysis.minimum_visible_size_current_streak}",
                f"timeline={timeline}",
                f"replay={str(report.idempotent_replay).lower()}",
            )
        )
    )


def _print_orderbook_history_report(report: OrderbookHistoryReport) -> None:
    analysis = report.analysis
    timeline = ",".join(
        f"{value.ordinal}:{value.state.value}:{value.continuity_state.value}:"
        f"{'+'.join(value.reason_codes) or '-'}"
        for value in report.observations
    )
    print(
        " ".join(
            (
                f"market_ticker={analysis.provider_market_ticker}",
                f"first_observed_at={report.observations[0].observed_at.isoformat()}",
                f"last_observed_at={report.observations[-1].observed_at.isoformat()}",
                f"observations={analysis.included_observation_count}",
                f"candidate={analysis.candidate_observation_count}",
                f"no_candidate={analysis.no_candidate_observation_count}",
                f"ineligible={analysis.ineligible_observation_count}",
                f"invalid={analysis.invalid_observation_count}",
                f"continuity_segments={analysis.continuity_segment_count}",
                f"evidence_breaks={analysis.evidence_break_count}",
                f"observation_gaps={analysis.observation_gap_count}",
                f"current_candidate_streak={analysis.current_candidate_streak_observation_count}",
                f"longest_candidate_streak={analysis.longest_candidate_streak_observation_count}",
                f"current_matched_quantity={analysis.current_matched_quantity}",
                f"maximum_compatible_matched_quantity={analysis.maximum_compatible_matched_quantity}",
                f"current_net_lower_bound={analysis.current_conservative_net_lower_bound}",
                f"maximum_compatible_net_lower_bound={analysis.maximum_compatible_conservative_net_lower_bound}",
                f"current_net_lower_bound_per_contract={analysis.current_net_lower_bound_per_contract}",
                f"timeline={timeline}",
                f"replay={str(report.idempotent_replay).lower()}",
            )
        )
    )


def _print_binary_complement_report(report: PublicBinaryComplementReport) -> None:
    priorities = ",".join(value.provider_market_ticker for value in report.ranked_priorities) or "-"
    reasons = (
        ",".join(
            f"{value.provider_market_ticker}:"
            f"{'+'.join(code.value for code in value.reason_codes) or '-'}"
            for value in report.evaluations
        )
        or "-"
    )
    print(
        " ".join(
            (
                f"markets_evaluated={report.markets_evaluated}",
                f"valid_orderbooks={report.valid_orderbooks}",
                f"invalid_orderbooks={report.invalid_orderbooks}",
                f"no_candidate={report.no_candidate_count}",
                f"ineligible={report.ineligible_count}",
                f"candidate={report.candidate_count}",
                f"matched_quantity={report.matched_quantity}",
                f"total_gross_gap={report.total_gross_gap}",
                f"total_modeled_fees={report.total_modeled_fees}",
                f"total_conservative_net_lower_bound={report.total_conservative_net_lower_bound}",
                f"shadow_executable_quality_priorities={priorities}",
                f"reason_codes={reasons}",
                f"replay={str(report.idempotent_replay).lower()}",
            )
        )
    )


def _print_capture_report(report: OrderbookCaptureReport) -> None:
    session = report.session
    print(
        " ".join(
            (
                f"session_id={session.session_id}",
                f"market={session.provider_market_ticker}",
                f"state={session.state.value}",
                f"attempted={session.attempted_observation_count}",
                f"successful={session.successful_observation_count}",
                f"failed={session.failed_observation_count}",
                f"history_analysis_id={session.history_analysis_id or 'none'}",
                f"last_error={session.last_error_code or 'none'}",
                f"replay={str(report.idempotent_replay).lower()}",
            )
        )
    )
    for observation in report.observations:
        print(
            " ".join(
                (
                    f"observation={observation.ordinal + 1}",
                    f"state={observation.state.value}",
                    f"reason={observation.result_code}",
                )
            )
        )


def _print_sweep_report(report: PublicKalshiOrderbookSweepReport) -> None:
    discovery = report.discovery
    print(
        " ".join(
            (
                f"sweep_id={report.sweep_id}",
                f"state={report.state.value}",
                f"discovery_markets={discovery.market_count if discovery else 'none'}",
                f"selected={report.selected_market_count}/{report.requested_market_count}",
                f"enrichment_attempts={report.enrichment_attempt_count}",
                f"failed_sessions={report.failed_session_count}",
                f"cohort_smaller_than_requested={str(report.selected_cohort_smaller_than_requested).lower()}",
                f"fatal_error={report.fatal_error_code or 'none'}",
            )
        )
    )
    for market in report.markets:
        states = ",".join(f"{state}:{count}" for state, count in market.evaluation_state_counts)
        reasons = "+".join(market.reason_codes) or "-"
        print(
            " ".join(
                (
                    f"market={market.provider_market_ticker}",
                    f"event={market.provider_event_ticker}",
                    f"series={market.series_ticker}",
                    f"session={market.session_state.value}",
                    f"attempted={market.attempted_observation_count}",
                    f"successful={market.successful_observation_count}",
                    f"shape={market.orderbook_shape.value if market.orderbook_shape else 'none'}",
                    f"evaluation_states={states or '-'}",
                    f"candidates={market.candidate_observation_count}",
                    f"longest_candidate_streak={market.longest_compatible_candidate_streak}",
                    f"reasons={reasons}",
                )
            )
        )
    ranking = (
        ",".join(
            f"{value.provider_market_ticker}:{value.label.value}"
            for value in report.research_ranking
        )
        or "-"
    )
    print(f"shadow_complement_research_ranking={ranking}")


def _print_threshold_ladder_report(report: PublicThresholdLadderScanReport) -> None:
    """Print retained threshold evidence without provider payloads or rule text."""

    scan = report.scan
    discovery = report.discovery
    state_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for evaluation in report.evaluations:
        state_counts[evaluation.state.value] = state_counts.get(evaluation.state.value, 0) + 1
        for reason in evaluation.reason_codes:
            reason_counts[reason.value] = reason_counts.get(reason.value, 0) + 1
    print(
        " ".join(
            (
                f"scan_id={scan.scan_id}",
                f"state={scan.state.value}",
                f"discovery_markets={discovery.market_count if discovery else 'retained'}",
                f"selected_events={scan.selected_event_count}",
                f"relationships={scan.relationship_count}",
                f"evaluations={scan.evaluation_count}",
                f"candidates={scan.candidate_count}",
                f"persisting_candidates={scan.persisting_candidate_count}",
                f"enrichment_attempts={report.enrichment_attempt_count}",
                f"replay={str(report.idempotent_replay).lower()}",
            )
        )
    )
    for event in report.events:
        print(
            " ".join(
                (
                    f"event={event.provider_event_ticker}",
                    f"series={event.series_ticker}",
                    f"markets={','.join(event.market_tickers)}",
                    f"verified_relationships={event.verified_relationship_count}",
                )
            )
        )
    for relationship in report.relationships:
        if relationship.state.value == "VERIFIED":
            direction = relationship.direction.value if relationship.direction else "none"
            print(
                " ".join(
                    (
                        "relationship=VERIFIED",
                        f"event={relationship.provider_event_ticker}",
                        f"broader={relationship.broader_market_ticker}",
                        f"narrower={relationship.narrower_market_ticker}",
                        f"direction={direction}",
                    )
                )
            )
    print(
        "evaluation_states="
        + (
            ",".join(f"{key}:{state_counts[key]}" for key in sorted(state_counts))
            if state_counts
            else "none"
        )
    )
    print(
        "reason_codes="
        + (
            ",".join(f"{key}:{reason_counts[key]}" for key in sorted(reason_counts))
            if reason_counts
            else "none"
        )
    )
    for result in report.ranking:
        print(
            " ".join(
                (
                    f"ranking={result.label.value}",
                    f"event={result.provider_event_ticker}",
                    f"broader={result.broader_market_ticker}",
                    f"narrower={result.narrower_market_ticker}",
                    f"candidate_rounds={result.candidate_round_count}",
                    f"matched_quantity={result.maximum_matched_quantity}",
                    f"conservative_net_lower_bound={result.maximum_conservative_net_lower_bound}",
                    f"reasons={'+'.join(result.reason_codes) or '-'}",
                )
            )
        )


def scan_kalshi_binary_complements_fixture() -> PublicBinaryComplementReport:
    market = fixture_market()
    return ScanPublicBinaryComplements(
        client=KalshiFixtureBinaryComplementClient(),
        store=InMemoryPublicBinaryComplementStore((market,)),
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    ).scan(
        BinaryComplementScanRequest(
            market_discovery_run_id=market.ingestion_run_id,
            market_limit=1,
            maximum_depth=5,
            maximum_bundle_quantity=parse_decimal("10"),
            timeout_seconds=10,
        )
    )


def smoke_kalshi_binary_complements(
    *,
    market_run_id: str | None,
    market_limit: int,
    maximum_depth: int,
    maximum_bundle_quantity: str,
    timeout_seconds: int,
    discover_first: bool,
) -> PublicBinaryComplementReport:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_public_binary_complement_store,
        postgres_public_market_discovery_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        market_store = postgres_public_market_discovery_store(engine)
        selected_run_id = market_run_id or market_store.latest_completed_run_id()
        if selected_run_id is None and discover_first:
            selected_run_id = (
                DiscoverPublicPredictionMarkets(
                    client=KalshiPublicMarketDataClient(),
                    normalizer=KalshiPublicMarketNormalizer(),
                    store=market_store,
                    clock=lambda: datetime.now(tz=UTC),
                )
                .discover(
                    PublicMarketScanRequest(
                        run_key="kalshi-binary-complement-discover-first-v1",
                        page_size=market_limit,
                        maximum_pages=1,
                        timeout_seconds=timeout_seconds,
                    )
                )
                .ingestion_run_id
            )
        if selected_run_id is None:
            raise PublicBinaryComplementError(
                "no completed persisted public market scan exists; use --discover-first"
            )
        return ScanPublicBinaryComplements(
            client=KalshiPublicOrderbookDataClient(),
            store=postgres_public_binary_complement_store(engine),
            clock=lambda: datetime.now(tz=UTC),
        ).scan(
            BinaryComplementScanRequest(
                market_discovery_run_id=selected_run_id,
                market_limit=market_limit,
                maximum_depth=maximum_depth,
                maximum_bundle_quantity=parse_decimal(maximum_bundle_quantity),
                timeout_seconds=timeout_seconds,
            )
        )
    finally:
        engine.dispose()


def scan_kalshi_fixture(*, persist: bool) -> PublicMarketScanSummary:
    store, engine = _discovery_store(persist=persist)
    try:
        service = DiscoverPublicPredictionMarkets(
            client=KalshiFixtureMarketDataClient(),
            normalizer=KalshiPublicMarketNormalizer(),
            store=store,
            clock=lambda: datetime(2026, 7, 20, 12, 4, tzinfo=UTC),
        )
        return service.discover(
            PublicMarketScanRequest(
                run_key="kalshi-public-fixture-v1",
                page_size=25,
                maximum_pages=3,
                timeout_seconds=10,
            )
        )
    finally:
        if engine is not None:
            engine.dispose()


def smoke_kalshi_public(
    *,
    limit: int,
    maximum_pages: int,
    timeout_seconds: int,
    persist: bool,
) -> PublicMarketScanSummary:
    store, engine = _discovery_store(persist=persist)
    try:
        service = DiscoverPublicPredictionMarkets(
            client=KalshiPublicMarketDataClient(),
            normalizer=KalshiPublicMarketNormalizer(),
            store=store,
            clock=lambda: datetime.now(tz=UTC),
        )
        return service.discover(
            PublicMarketScanRequest(
                run_key="kalshi-public-smoke-v1",
                page_size=limit,
                maximum_pages=maximum_pages,
                timeout_seconds=timeout_seconds,
            )
        )
    finally:
        if engine is not None:
            engine.dispose()


def _consistency_stores(
    *, persist: bool
) -> tuple[PublicMarketDiscoveryStore, PublicEventConsistencyStore, _Disposable | None]:
    if not persist:
        market_store = InMemoryPublicMarketDiscoveryStore()
        return market_store, InMemoryPublicEventConsistencyStore(market_store), None
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_public_event_consistency_store,
        postgres_public_market_discovery_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        postgres_public_market_discovery_store(engine),
        postgres_public_event_consistency_store(engine),
        engine,
    )


def _scan_kalshi_consistency(
    *,
    market_client: PublicMarketPageClient,
    event_client: PublicEventPageClient,
    market_run_key: str,
    event_run_key: str,
    market_limit: int,
    event_limit: int,
    maximum_pages: int,
    timeout_seconds: int,
    persist: bool,
) -> PublicEventConsistencyScanSummary:
    if not 1 <= market_limit <= 1000 or not 1 <= event_limit <= 500:
        raise ValueError("public consistency limits are out of bounds")
    market_store, event_store, engine = _consistency_stores(persist=persist)
    try:

        def clock() -> datetime:
            return datetime.now(tz=UTC)

        market_summary = DiscoverPublicPredictionMarkets(
            client=market_client,
            normalizer=KalshiPublicMarketNormalizer(),
            store=market_store,
            clock=clock,
        ).discover(
            PublicMarketScanRequest(
                run_key=market_run_key,
                page_size=market_limit,
                maximum_pages=maximum_pages,
                timeout_seconds=timeout_seconds,
            )
        )
        return DiscoverPublicEventConsistency(
            client=event_client,
            normalizer=KalshiPublicEventNormalizer(),
            store=event_store,
            clock=clock,
        ).discover(
            PublicEventScanRequest(
                run_key=event_run_key,
                page_size=min(event_limit, 200),
                event_limit=event_limit,
                maximum_pages=maximum_pages,
                timeout_seconds=timeout_seconds,
                market_ingestion_run_id=market_summary.ingestion_run_id,
            )
        )
    finally:
        if engine is not None:
            engine.dispose()


def scan_kalshi_consistency_fixture(*, persist: bool) -> PublicEventConsistencyScanSummary:
    client = KalshiFixturePublicConsistencyClient()
    return _scan_kalshi_consistency(
        market_client=client,
        event_client=client,
        market_run_key="kalshi-consistency-fixture-markets-v1",
        event_run_key="kalshi-consistency-fixture-events-v1",
        market_limit=25,
        event_limit=25,
        maximum_pages=3,
        timeout_seconds=10,
        persist=persist,
    )


def smoke_kalshi_consistency(
    *,
    event_limit: int,
    market_limit: int,
    maximum_pages: int,
    timeout_seconds: int,
    persist: bool,
) -> PublicEventConsistencyScanSummary:
    return _scan_kalshi_consistency(
        market_client=KalshiPublicMarketDataClient(),
        event_client=KalshiPublicEventDataClient(),
        market_run_key="kalshi-consistency-public-markets-v1",
        event_run_key="kalshi-consistency-public-events-v1",
        market_limit=market_limit,
        event_limit=event_limit,
        maximum_pages=maximum_pages,
        timeout_seconds=timeout_seconds,
        persist=persist,
    )


def compare_kalshi_scans_fixture() -> PublicScanComparisonReport:
    baseline, current = temporal_fixture_scans()
    return ComparePublicMarketScans(
        store=InMemoryPublicScanComparisonStore((baseline, current))
    ).compare(
        baseline_ingestion_run_id=baseline.ingestion_run_id,
        current_ingestion_run_id=current.ingestion_run_id,
    )


def _persisted_scan_comparison_service() -> tuple[ComparePublicMarketScans, _Disposable]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_public_scan_comparison_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return ComparePublicMarketScans(store=postgres_public_scan_comparison_store(engine)), engine


def compare_kalshi_scans(
    *, baseline_run_id: str, current_run_id: str
) -> PublicScanComparisonReport:
    service, engine = _persisted_scan_comparison_service()
    try:
        return service.compare(
            baseline_ingestion_run_id=baseline_run_id,
            current_ingestion_run_id=current_run_id,
        )
    finally:
        engine.dispose()


def compare_kalshi_latest_scans() -> PublicScanComparisonReport:
    service, engine = _persisted_scan_comparison_service()
    try:
        return service.compare_latest()
    finally:
        engine.dispose()


def summarize_kalshi_event_history_fixture() -> EventHistoryReport:
    return BuildEventObservationHistory(
        store=InMemoryEventObservationHistoryStore(temporal_history_fixture_runs())
    ).build(EventHistoryRequest(provider_event_ticker="HISTORY-EVENT", maximum_completed_runs=9))


def summarize_kalshi_event_history(
    *,
    event_ticker: str,
    maximum_completed_runs: int,
    first_ingestion_run_id: str | None,
    last_ingestion_run_id: str | None,
    continuity_gap_seconds: int,
) -> EventHistoryReport:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_public_event_history_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        return BuildEventObservationHistory(
            store=postgres_public_event_history_store(engine)
        ).build(
            EventHistoryRequest(
                provider_event_ticker=event_ticker,
                first_ingestion_run_id=first_ingestion_run_id,
                last_ingestion_run_id=last_ingestion_run_id,
                maximum_completed_runs=maximum_completed_runs,
                continuity_gap=timedelta(seconds=continuity_gap_seconds),
            )
        )
    finally:
        engine.dispose()


def summarize_kalshi_orderbook_history_fixture() -> OrderbookHistoryReport:
    return BuildOrderbookObservationHistory(
        store=InMemoryOrderbookObservationHistoryStore(temporal_orderbook_history_fixture())
    ).build(
        OrderbookHistoryRequest(
            provider_market_ticker=ORDERBOOK_HISTORY_MARKET_TICKER,
            maximum_observations=10,
        )
    )


def summarize_kalshi_orderbook_history(
    *,
    market_ticker: str,
    maximum_observations: int,
    first_orderbook_snapshot_id: str | None,
    last_orderbook_snapshot_id: str | None,
    continuity_gap_seconds: int,
) -> OrderbookHistoryReport:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_orderbook_observation_history_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        return BuildOrderbookObservationHistory(
            store=postgres_orderbook_observation_history_store(engine)
        ).build(
            OrderbookHistoryRequest(
                provider_market_ticker=market_ticker,
                first_orderbook_snapshot_id=first_orderbook_snapshot_id,
                last_orderbook_snapshot_id=last_orderbook_snapshot_id,
                maximum_observations=maximum_observations,
                continuity_gap=timedelta(seconds=continuity_gap_seconds),
            )
        )
    finally:
        engine.dispose()


def capture_kalshi_orderbook_series_fixture() -> OrderbookCaptureReport:
    clock = VirtualCaptureClock()
    progress: list[str] = []
    report = CaptureBoundedPublicOrderbookSession(
        client=KalshiFixtureOrderbookCaptureClient(clock),
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
        progress=progress.append,
    ).capture(
        BoundedOrderbookCaptureRequest(
            session_id="fixture-bounded-orderbook-capture-v1",
            market_ticker=CAPTURE_FIXTURE_MARKET_TICKER,
            observations=5,
            interval_seconds=5,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            timeout_seconds=10,
        )
    )
    for line in progress:
        print(line)
    return report


def capture_kalshi_orderbook_series(
    *,
    market_ticker: str | None,
    latest_open_market: bool,
    discover_first: bool,
    observations: int,
    interval_seconds: int,
    maximum_depth: int,
    maximum_bundle_quantity: str,
    timeout_seconds: int,
) -> OrderbookCaptureReport:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_public_market_discovery_store,
    )

    started_at = datetime.now(tz=UTC)
    session_id = deterministic_id(
        "orderbook_capture_session",
        {
            "started_at": started_at,
            "market_ticker": market_ticker,
            "latest_open_market": latest_open_market,
            "observations": observations,
            "interval_seconds": interval_seconds,
            "maximum_depth": maximum_depth,
            "maximum_bundle_quantity": maximum_bundle_quantity,
            "timeout_seconds": timeout_seconds,
        },
    )
    request = BoundedOrderbookCaptureRequest(
        session_id=session_id,
        market_ticker=market_ticker,
        latest_open_market=latest_open_market,
        discover_first=discover_first,
        observations=observations,
        interval_seconds=interval_seconds,
        maximum_depth=maximum_depth,
        maximum_bundle_quantity=maximum_bundle_quantity,
        timeout_seconds=timeout_seconds,
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        discovery_store = postgres_public_market_discovery_store(engine)

        def discover(session_key: str, timeout: int) -> tuple[CaptureMarketCandidate, ...]:
            summary = DiscoverPublicPredictionMarkets(
                client=KalshiPublicMarketDataClient(),
                normalizer=KalshiPublicMarketNormalizer(),
                store=discovery_store,
                clock=lambda: datetime.now(tz=UTC),
            ).discover(
                PublicMarketScanRequest(
                    run_key=f"bounded-capture-discover-{session_key}",
                    page_size=100,
                    maximum_pages=3,
                    timeout_seconds=timeout,
                )
            )
            return capture_store.select_capture_candidates(
                ingestion_run_id=summary.ingestion_run_id, limit=10
            )

        return CaptureBoundedPublicOrderbookSession(
            client=KalshiPublicOrderbookDataClient(),
            store=capture_store,
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            normalizer=KalshiPublicMarketNormalizer(),
            progress=print,
            discover_first=discover,
        ).capture(request)
    finally:
        engine.dispose()


def sweep_kalshi_orderbooks_fixture() -> PublicKalshiOrderbookSweepReport:
    clock = VirtualCaptureClock()
    progress: list[str] = []
    client = KalshiFixtureOrderbookSweepClient(clock)
    discovery_store = InMemoryPublicMarketDiscoveryStore()
    capture_store = InMemoryPublicOrderbookCaptureStore()
    capture = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=capture_store,
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
        progress=progress.append,
    )
    report = SweepPublicKalshiOrderbooks(
        discovery=DiscoverPublicPredictionMarkets(
            client=client,
            normalizer=KalshiPublicMarketNormalizer(),
            store=discovery_store,
            clock=clock.wall_clock,
        ),
        capture=capture,
        candidate_selector=lambda run_id, limit: fixture_capture_candidates(
            discovery_store, run_id, limit
        ),
        progress=progress.append,
    ).sweep(
        PublicKalshiOrderbookSweepRequest(
            sweep_id="fixture-public-kalshi-orderbook-sweep-v1",
            market_count=13,
            maximum_pages=3,
            market_limit=50,
            maximum_enrichments=20,
            observations=3,
            interval_seconds=5,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            timeout_seconds=10,
        )
    )
    for line in progress:
        print(line)
    return report


def sweep_kalshi_orderbooks(
    *,
    market_count: int,
    maximum_pages: int,
    market_limit: int,
    maximum_enrichments: int,
    observations: int,
    interval_seconds: int,
    maximum_depth: int,
    maximum_bundle_quantity: str,
    timeout_seconds: int,
) -> PublicKalshiOrderbookSweepReport:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_public_market_discovery_store,
    )

    started_at = datetime.now(tz=UTC)
    request = PublicKalshiOrderbookSweepRequest(
        sweep_id=deterministic_id(
            "public_kalshi_orderbook_sweep",
            {
                "started_at": started_at,
                "market_count": market_count,
                "maximum_pages": maximum_pages,
                "market_limit": market_limit,
                "maximum_enrichments": maximum_enrichments,
                "observations": observations,
                "interval_seconds": interval_seconds,
                "maximum_depth": maximum_depth,
                "maximum_bundle_quantity": maximum_bundle_quantity,
                "timeout_seconds": timeout_seconds,
            },
        ),
        market_count=market_count,
        maximum_pages=maximum_pages,
        market_limit=market_limit,
        maximum_enrichments=maximum_enrichments,
        observations=observations,
        interval_seconds=interval_seconds,
        maximum_depth=maximum_depth,
        maximum_bundle_quantity=maximum_bundle_quantity,
        timeout_seconds=timeout_seconds,
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        capture = CaptureBoundedPublicOrderbookSession(
            client=KalshiPublicOrderbookDataClient(),
            store=capture_store,
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            normalizer=KalshiPublicMarketNormalizer(),
            progress=print,
        )
        return SweepPublicKalshiOrderbooks(
            discovery=DiscoverPublicPredictionMarkets(
                client=KalshiPublicMarketDataClient(),
                normalizer=KalshiPublicMarketNormalizer(),
                store=postgres_public_market_discovery_store(engine),
                clock=lambda: datetime.now(tz=UTC),
            ),
            capture=capture,
            candidate_selector=lambda run_id, limit: capture_store.select_capture_candidates(
                ingestion_run_id=run_id, limit=limit
            ),
            progress=print,
        ).sweep(request)
    finally:
        engine.dispose()


def scan_kalshi_threshold_ladders_fixture() -> PublicThresholdLadderScanReport:
    return threshold_ladder_fixture()


def scan_kalshi_threshold_ladders(
    *,
    scan_id: str | None = None,
    event_count: int,
    maximum_pages: int,
    market_limit: int,
    maximum_enrichments: int,
    maximum_markets_per_event: int,
    maximum_relationships: int,
    rounds: int,
    interval_seconds: int,
    maximum_depth: int,
    maximum_bundle_quantity: str,
    maximum_observation_skew_seconds: int,
    timeout_seconds: int,
) -> PublicThresholdLadderScanReport:
    """Run one foreground public-only threshold scan against durable evidence stores."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_public_market_discovery_store,
        postgres_public_threshold_ladder_store,
    )

    started_at = datetime.now(tz=UTC)
    request = PublicThresholdLadderScanRequest(
        scan_id=scan_id
        or deterministic_id(
            "public_kalshi_threshold_ladder_scan",
            {
                "started_at": started_at,
                "event_count": event_count,
                "maximum_pages": maximum_pages,
                "market_limit": market_limit,
                "maximum_enrichments": maximum_enrichments,
                "maximum_markets_per_event": maximum_markets_per_event,
                "maximum_relationships": maximum_relationships,
                "rounds": rounds,
                "interval_seconds": interval_seconds,
                "maximum_depth": maximum_depth,
                "maximum_bundle_quantity": maximum_bundle_quantity,
                "maximum_observation_skew_seconds": maximum_observation_skew_seconds,
                "timeout_seconds": timeout_seconds,
            },
        ),
        event_count=event_count,
        maximum_pages=maximum_pages,
        market_limit=market_limit,
        maximum_enrichments=maximum_enrichments,
        maximum_markets_per_event=maximum_markets_per_event,
        maximum_relationships=maximum_relationships,
        rounds=rounds,
        interval_seconds=interval_seconds,
        maximum_depth=maximum_depth,
        maximum_bundle_quantity=maximum_bundle_quantity,
        maximum_observation_skew_seconds=maximum_observation_skew_seconds,
        timeout_seconds=timeout_seconds,
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        normalizer = KalshiPublicMarketNormalizer()
        return ScanPublicThresholdLadders(
            discovery=DiscoverPublicPredictionMarkets(
                client=KalshiPublicMarketDataClient(),
                normalizer=normalizer,
                store=postgres_public_market_discovery_store(engine),
                clock=lambda: datetime.now(tz=UTC),
            ),
            capture=CaptureBoundedPublicOrderbookSession(
                client=KalshiPublicOrderbookDataClient(),
                store=capture_store,
                wall_clock=lambda: datetime.now(tz=UTC),
                monotonic_clock=time.monotonic,
                sleeper=time.sleep,
                normalizer=normalizer,
            ),
            client=KalshiPublicOrderbookDataClient(),
            candidate_selector=lambda run_id, limit: capture_store.select_capture_candidates(
                ingestion_run_id=run_id, limit=limit
            ),
            store=postgres_public_threshold_ladder_store(engine),
            normalizer=normalizer,
            event_normalizer=KalshiPublicEventNormalizer(),
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            progress=print,
        ).scan(request)
    finally:
        engine.dispose()


def diagnose_kalshi_capture_selection(
    *, max_pages: int, market_limit: int, timeout_seconds: int
) -> None:
    """Retain one bounded discovery run and print capture-selection evidence only."""

    if not 1 <= max_pages <= 3:
        raise PublicOrderbookCaptureError("capture diagnostic pages must be between one and three")
    if not 1 <= market_limit <= 300:
        raise PublicOrderbookCaptureError(
            "capture diagnostic market limit must be between one and 300"
        )
    if not 1 <= timeout_seconds <= 30:
        raise PublicOrderbookCaptureError("capture diagnostic timeout must be between one and 30")
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_public_market_discovery_store,
    )

    started_at = datetime.now(tz=UTC)
    run_key = deterministic_id(
        "kalshi_capture_selection_diagnostic",
        {
            "started_at": started_at,
            "max_pages": max_pages,
            "market_limit": market_limit,
            "timeout_seconds": timeout_seconds,
        },
    )
    page_size = max(1, market_limit // max_pages)
    effective_pages = min(max_pages, market_limit // page_size)
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        discovery_store = postgres_public_market_discovery_store(engine)
        summary = DiscoverPublicPredictionMarkets(
            client=KalshiPublicMarketDataClient(),
            normalizer=KalshiPublicMarketNormalizer(),
            store=discovery_store,
            clock=lambda: datetime.now(tz=UTC),
        ).discover(
            PublicMarketScanRequest(
                run_key=run_key,
                page_size=page_size,
                maximum_pages=effective_pages,
                timeout_seconds=timeout_seconds,
            )
        )
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        diagnostics = capture_store.selection_diagnostics(ingestion_run_id=summary.ingestion_run_id)
        candidates = capture_store.select_capture_candidates(
            ingestion_run_id=summary.ingestion_run_id, limit=10
        )
        enrichment = CaptureBoundedPublicOrderbookSession(
            client=KalshiPublicOrderbookDataClient(),
            store=capture_store,
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            normalizer=KalshiPublicMarketNormalizer(),
        ).enrich_candidates(
            request=BoundedOrderbookCaptureRequest(
                session_id=deterministic_id(
                    "kalshi_capture_selection_diagnostic",
                    {"run_id": summary.ingestion_run_id},
                ),
                latest_open_market=True,
                observations=3,
                interval_seconds=5,
                maximum_depth=5,
                maximum_bundle_quantity="10",
                timeout_seconds=timeout_seconds,
            ),
            candidates=candidates,
        )
    finally:
        engine.dispose()
    print(
        " ".join(
            (
                f"run_id={summary.ingestion_run_id}",
                f"pages={summary.page_count}",
                f"markets_received={diagnostics.provider_market_count}",
                f"markets_parsed={diagnostics.successfully_parsed_count}",
                f"structurally_invalid={diagnostics.structurally_invalid_count}",
                f"lifecycle_ineligible={diagnostics.lifecycle_ineligible_count}",
                f"unsupported_market_structure={diagnostics.unsupported_market_structure_count}",
                f"missing_rule_lineage={diagnostics.missing_rule_lineage_count}",
                "structurally_eligible_pre_enrichment="
                f"{diagnostics.structurally_eligible_pre_enrichment_count}",
                f"list_series_identity_absent={diagnostics.list_series_identity_absent_count}",
                f"event_enrichments_attempted={enrichment.event_enrichments_attempted}",
                f"event_enrichments_succeeded={enrichment.event_enrichments_succeeded}",
                f"event_membership_failures={enrichment.event_membership_failures}",
                f"series_enrichments_succeeded={enrichment.series_enrichments_succeeded}",
                f"fee_evidence_failures={enrichment.fee_evidence_failures}",
                f"exact_market_refresh_successes={enrichment.exact_market_refresh_successes}",
                f"final_capture_eligible={int(enrichment.selected is not None)}",
            )
        )
    )
    print(
        "pre_enrichment_tickers="
        + (
            ",".join(diagnostics.pre_enrichment_tickers)
            if diagnostics.pre_enrichment_tickers
            else "none"
        )
    )
    print(
        "selected_ticker="
        + (enrichment.selected.market.provider_market_ticker if enrichment.selected else "none")
    )
    print(
        "rejection_reasons="
        + (
            ",".join(
                f"{reason}:{count}"
                for reason, count in (*diagnostics.reason_counts, *enrichment.rejection_counts)
            )
            if diagnostics.reason_counts or enrichment.rejection_counts
            else "none"
        )
    )


def reconcile_kalshi_capture_sessions() -> tuple[str, ...]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        sessions = postgres_bounded_orderbook_capture_store(engine).reconcile_stale(
            now=datetime.now(tz=UTC), grace=timedelta(seconds=30)
        )
        return tuple(value.session_id for value in sessions)
    finally:
        engine.dispose()


def _autopilot_store() -> tuple[PostgresAutopilotStore, _Disposable]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_autopilot_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return postgres_autopilot_store(engine), engine


def _print_autopilot(value: object) -> None:
    print(canonical_json_bytes(value).decode("utf-8"))


def _autopilot_cycle_summary(value: AutopilotCycleResult) -> dict[str, object]:
    return {
        "cycle_id": value.cycle_id,
        "campaign_id": value.campaign_id,
        "state": value.state,
        "idempotent_replay": value.idempotent_replay,
        "invocation_count": value.invocation_count,
        "assessment_count": value.assessment_count,
        "approved_count": value.approved_count,
        "rejected_count": value.rejected_count,
        "assessments": tuple(
            {
                "proposal_id": assessment.opportunity.proposal.proposal_id,
                "validity": assessment.validity,
                "decision": assessment.decision,
                "ranking_ordinal": assessment.ranking_ordinal,
                "reason_codes": assessment.reason_codes,
            }
            for assessment in value.assessments
        ),
    }


def autopilot_strategies() -> tuple[object, ...]:
    store, engine = _autopilot_store()
    try:
        store.register_descriptors(governed_strategy_descriptors())
        return tuple(
            {
                "strategy_id": descriptor.strategy_id,
                "version": descriptor.version,
                "family": descriptor.family,
                "implementation_state": descriptor.implementation_state,
                "authority": descriptor.authority_state,
                "evidence_state": descriptor.evidence_state,
                "last_evidence_decision": descriptor.last_evidence_decision,
                "runnable": descriptor.runnable,
            }
            for descriptor in store.strategy_catalog()
        )
    finally:
        engine.dispose()


def autopilot_status(campaign_id: str) -> dict[str, object]:
    store, engine = _autopilot_store()
    try:
        store.register_descriptors(governed_strategy_descriptors())
        return store.status(campaign_id)
    finally:
        engine.dispose()


def create_shadow_campaign(
    *, name: str, capital: str, maximum_loss: str, expires_in_days: int, idempotency_key: str
) -> dict[str, object]:
    """Create one durable RUNNING SHADOW campaign through the existing service."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_operator_console_store,
    )
    from apex.application.operator_console import (
        CreateMoonshotCampaign,
        CreateMoonshotCampaignCommand,
        OperatorMode,
    )

    if not 1 <= expires_in_days <= 365:
        raise ValueError("shadow campaign expiration must be between one and 365 days")
    now = datetime.now(tz=UTC)
    campaign_id = deterministic_id("forward_shadow_campaign", {"idempotency_key": idempotency_key})
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_operator_console_store(engine)
        if store.campaign_metadata(campaign_id) is not None:
            return {"campaign_id": campaign_id, "idempotent_replay": True}
        return CreateMoonshotCampaign(store).create(
            CreateMoonshotCampaignCommand(
                campaign_id=campaign_id,
                name=name,
                initial_capital=Money(capital),
                maximum_authorized_loss=Money(maximum_loss),
                starts_at=now,
                expires_at=now + timedelta(days=expires_in_days),
                operating_mode=OperatorMode.SHADOW,
                maximum_active_risk_fraction=RiskFraction("0.25"),
                maximum_per_opportunity_risk_fraction=RiskFraction("0.02"),
                maximum_per_strategy_risk_fraction=RiskFraction("0.10"),
                maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.05"),
                maximum_simultaneous_reservations=0,
                maximum_simultaneous_positions=0,
                maximum_capital_lockup_seconds=3600,
                minimum_conservative_expected_value=Money("0"),
                minimum_evidence_tier=EvidenceTier.RESEARCH,
                allow_profit_compounding=False,
                operator_note="Forward market-dynamics validation; SHADOW only and no execution.",
                idempotency_key=idempotency_key,
            )
        )
    finally:
        engine.dispose()


def autopilot_cycle(
    *,
    campaign_id: str,
    mode: str,
    idempotency_key: str,
    maximum_strategies: int,
    maximum_proposals: int,
    deadline_seconds: int,
    operator_note: str,
) -> AutopilotCycleResult:
    if deadline_seconds <= 0:
        raise ValueError("Autopilot deadline must be positive")
    store, engine = _autopilot_store()
    try:
        store.register_descriptors(governed_strategy_descriptors())
        requested_at = datetime.now(tz=UTC)
        command = AutopilotCycleCommand(
            campaign_id=campaign_id,
            idempotency_key=idempotency_key,
            maximum_strategies=maximum_strategies,
            maximum_proposals=maximum_proposals,
            deadline_at=requested_at + timedelta(seconds=deadline_seconds),
            operator_note=operator_note,
            requested_at=requested_at,
            authority=(
                AutopilotCycleAuthority.PAPER if mode == "paper" else AutopilotCycleAuthority.SHADOW
            ),
        )
        if mode == "shadow":
            return RunAutopilotShadowCycle(store, StrategyRegistry(())).run(command)
        from apex.adapters.postgres import (
            postgres_evidence_review_store,
            postgres_paper_portfolio_store,
        )

        policy_payload = {"campaign_id": campaign_id, "policy": "cli-paper-v1"}
        policy = PaperExecutionPolicy(
            policy_version_id=f"{campaign_id}-cli-paper-v1",
            execution_model=PaperExecutionModel.IMMEDIATE_CROSS_V1,
            maximum_evidence_age_seconds=60,
            maximum_spread=Decimal("1"),
            maximum_orderbook_depth=5,
            maximum_visible_size_participation=RiskFraction("1"),
            slippage_reserve_per_contract=Money("0"),
            fee_model_version="paper-cli-v1",
            time_in_force_seconds=30,
            unfilled_behavior=PaperUnfilledBehavior.EXPIRE,
            configuration_version="paper-cli-v1",
            configuration_hash=ContentHash(content_digest(policy_payload)),
        )
        return RunAutopilotPaperCycle(
            store,
            StrategyRegistry(()),
            postgres_paper_portfolio_store(cast(Any, engine)),
            postgres_evidence_review_store(cast(Any, engine)),
        ).run(PaperCycleCommand(cycle=command, execution_policy=policy))
    finally:
        engine.dispose()


def _review_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("review timestamp must be ISO-8601 with a timezone") from error
    if parsed.tzinfo is None:
        raise ValueError("review timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def strategy_evidence_snapshot(
    *, strategy_id: str, configuration_hash: str, policy_id: str, cutoff: str
) -> dict[str, object]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_evidence_review_store,
    )
    from apex.application.evidence_review import (
        BuildStrategyEvidenceSnapshot,
        BuildStrategyEvidenceSnapshotCommand,
        initial_evidence_review_policies,
    )

    cutoff_at = _review_timestamp(cutoff)
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_evidence_review_store(engine)
        for policy in initial_evidence_review_policies(datetime(2026, 1, 1, tzinfo=UTC)):
            store.ensure_policy(policy)
        descriptor = store.descriptor(strategy_id)
        snapshot, criteria = BuildStrategyEvidenceSnapshot(store).build(
            BuildStrategyEvidenceSnapshotCommand(
                strategy_id=strategy_id,
                strategy_version=descriptor.version,
                configuration_hash=ContentHash(configuration_hash),
                policy_id=policy_id,
                policy_version="apex-evidence-review-policy-v1",
                cutoff_at=cutoff_at,
                full_history_started_at=cutoff_at - timedelta(days=365),
                recent_history_started_at=cutoff_at - timedelta(days=30),
                idempotency_key=deterministic_id(
                    "strategy_evidence_snapshot_cli",
                    {
                        "strategy_id": strategy_id,
                        "configuration_hash": configuration_hash,
                        "policy_id": policy_id,
                        "cutoff": cutoff_at,
                    },
                ),
            )
        )
        return {"snapshot": snapshot, "criteria": criteria}
    finally:
        engine.dispose()


def request_strategy_review(
    *, strategy_id: str, campaign_id: str, snapshot_id: str, reason: str, idempotency_key: str
) -> object:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_evidence_review_store,
    )
    from apex.application.evidence_review import (
        RequestStrategyEvidenceReview,
        RequestStrategyEvidenceReviewCommand,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_evidence_review_store(engine)
        descriptor = store.descriptor(strategy_id)
        return RequestStrategyEvidenceReview(store).request(
            RequestStrategyEvidenceReviewCommand(
                strategy_id=strategy_id,
                strategy_version=descriptor.version,
                snapshot_id=snapshot_id,
                campaign_id=campaign_id,
                expires_at=datetime.now(tz=UTC) + timedelta(days=7),
                operator_reason=reason,
                operator_note="CLI evidence-review request",
                idempotency_key=idempotency_key,
            ),
            now=datetime.now(tz=UTC),
        )
    finally:
        engine.dispose()


def strategy_review_status(*, review_id: str) -> object:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_evidence_review_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_evidence_review_store(engine)
        review = store.review(review_id)
        return {
            "review": review,
            "snapshot": store.snapshot(review.snapshot_id),
            "criteria": store.criteria(review.snapshot_id),
        }
    finally:
        engine.dispose()


def approve_paper_trial(
    *,
    review_id: str,
    campaign_id: str,
    starts_at: str,
    expires_at: str,
    maximum_cycles: int,
    maximum_orders: int,
    maximum_positions: int,
    maximum_loss: str,
    confirmation_strategy: str,
    confirmation_campaign: str,
    confirmation_snapshot: str,
    idempotency_key: str,
) -> object:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_evidence_review_store,
    )
    from apex.application.evidence_review import (
        DecideStrategyEvidenceReview,
        DecideStrategyEvidenceReviewCommand,
        PaperTrialLimits,
        StrategyEvidenceReviewState,
        paper_trial_confirmation_text,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_evidence_review_store(engine)
        review = store.review(review_id)
        if campaign_id != review.requested_campaign_id:
            raise ValueError("approval campaign must exactly match the requested review campaign")
        confirmation = paper_trial_confirmation_text(
            strategy_id=confirmation_strategy,
            campaign_id=confirmation_campaign,
            snapshot_id=confirmation_snapshot,
        )
        policy_id = f"{campaign_id}-cli-paper-v1"
        return DecideStrategyEvidenceReview(store).decide(
            DecideStrategyEvidenceReviewCommand(
                review_id=review_id,
                decision=StrategyEvidenceReviewState.APPROVED,
                operator_reason="CLI bounded PAPER trial approval",
                operator_note="CLI governed PAPER trial",
                idempotency_key=idempotency_key,
                confirmation_text=confirmation,
            ),
            now=datetime.now(tz=UTC),
            trial_limits=PaperTrialLimits(
                paper_execution_policy_id=policy_id,
                paper_execution_policy_hash=ContentHash(
                    content_digest({"campaign_id": campaign_id, "policy": "cli-paper-v1"})
                ),
                starts_at=_review_timestamp(starts_at),
                expires_at=_review_timestamp(expires_at),
                maximum_paper_cycles=maximum_cycles,
                maximum_approved_opportunities=maximum_orders,
                maximum_paper_orders=maximum_orders,
                maximum_simultaneous_open_positions=maximum_positions,
                maximum_per_opportunity_risk_fraction=RiskFraction("0.01"),
                maximum_strategy_active_risk_fraction=RiskFraction("0.05"),
                maximum_cumulative_authorized_risk=Money(maximum_loss),
                maximum_cumulative_realized_paper_loss=Money(maximum_loss),
                maximum_paper_drawdown=Money(maximum_loss),
                maximum_capital_lockup_seconds=3600,
                allowed_instrument_class="BOUNDED_PREDICTION_MARKET",
                allowed_provider="KALSHI",
            ),
        )
    finally:
        engine.dispose()


def evidence_review_paper_trial_demo() -> dict[str, object]:
    """Run the complete local, fixture-only governed PAPER proof.

    The retained market/outcome records are generated Kalshi-shaped fixtures;
    this command never calls a network provider.  Every review, authority,
    trial, Autopilot, authorization, modeled execution, and settlement step
    uses the same production service composition as the operator API.
    """

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        durable_order,
        normalized_market_snapshot,
        paper_execution,
        paper_position_lineage,
        position,
        postgres_autopilot_store,
        postgres_evidence_review_store,
        postgres_operator_console_store,
        postgres_paper_portfolio_store,
        postgres_public_market_discovery_store,
        postgres_shadow_operations_store,
        postgres_strategy_shadow_store,
    )
    from apex.application.autopilot import AutopilotIdempotencyConflictError
    from apex.application.evidence_review import (
        BuildStrategyEvidenceSnapshot,
        BuildStrategyEvidenceSnapshotCommand,
        DecideStrategyEvidenceReview,
        DecideStrategyEvidenceReviewCommand,
        PaperTrialAuthorizationState,
        PaperTrialLimits,
        RequestStrategyEvidenceReview,
        RequestStrategyEvidenceReviewCommand,
        StartStrategyEvidenceReview,
        StrategyEvidenceReviewPolicy,
        StrategyEvidenceReviewState,
        hard_gate_reasons,
        paper_trial_confirmation_text,
    )
    from apex.application.operator_console import (
        CreateMoonshotCampaign,
        CreateMoonshotCampaignCommand,
        OperatorMode,
    )
    from apex.application.shadow_operations import PublicOutcomeState

    campaign_id = "evidence-review-paper-trial-demo-v7"
    now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    registry = fixture_governed_evidence_registry()
    descriptor = registry.descriptors[0]
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        console = postgres_operator_console_store(engine)
        CreateMoonshotCampaign(console).create(
            CreateMoonshotCampaignCommand(
                campaign_id=campaign_id,
                name="Governed evidence-review fixture demonstration",
                initial_capital=Money("1000"),
                maximum_authorized_loss=Money("500"),
                starts_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(days=1),
                operating_mode=OperatorMode.PAPER,
                maximum_active_risk_fraction=RiskFraction("0.50"),
                maximum_per_opportunity_risk_fraction=RiskFraction("0.20"),
                maximum_per_strategy_risk_fraction=RiskFraction("0.50"),
                maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.50"),
                maximum_simultaneous_reservations=4,
                maximum_simultaneous_positions=4,
                maximum_capital_lockup_seconds=3600,
                minimum_conservative_expected_value=Money("0"),
                minimum_evidence_tier=EvidenceTier.RESEARCH,
                allow_profit_compounding=False,
                operator_note="fixture-only governed PAPER demonstration; no provider authority",
                idempotency_key="evidence-review-paper-trial-demo-campaign-v6",
            )
        )
        autopilot_store = postgres_autopilot_store(engine)
        autopilot_store.register_descriptors(registry.descriptors)
        paper_store = postgres_paper_portfolio_store(engine)
        execution_policy = _demo_paper_execution_policy()
        paper_store.ensure_execution_policy(execution_policy, recorded_at=now)

        discovery = postgres_public_market_discovery_store(engine)
        discovery_summary = DiscoverPublicPredictionMarkets(
            client=EvidenceReviewFixtureMarketClient(
                count=100, received_at=now - timedelta(days=2)
            ),
            normalizer=KalshiPublicMarketNormalizer(),
            store=discovery,
            clock=lambda: now - timedelta(days=2),
        ).discover(
            PublicMarketScanRequest(
                run_key="fixture-governed-evidence-markets-v7",
                page_size=100,
                maximum_pages=1,
                timeout_seconds=1,
            )
        )
        snapshot_ids = discovery.market_snapshot_ids_for_run(
            discovery_summary.ingestion_run_id, limit=100
        )
        if len(snapshot_ids) != 100:
            raise RuntimeError("fixture governed evidence did not retain 100 market snapshots")
        with cast(Any, engine).connect() as connection:
            market_rows = tuple(
                connection.execute(
                    normalized_market_snapshot.select()
                    .where(normalized_market_snapshot.c.snapshot_id.in_(snapshot_ids))
                    .order_by(normalized_market_snapshot.c.provider_market_ticker)
                ).mappings()
            )
        shadow_store = postgres_strategy_shadow_store(engine)
        outcomes: dict[str, str] = {}
        for ordinal, market in enumerate(market_rows):
            prediction, outcome = fixture_governed_forward_prediction(
                campaign_id=campaign_id,
                market_snapshot_id=str(market["snapshot_id"]),
                market_ticker=str(market["provider_market_ticker"]),
                event_ticker=str(market["event_ticker"]),
                decision_at=now - timedelta(days=2),
                ordinal=ordinal,
            )
            shadow_store.persist_prediction(prediction)
            outcomes[str(market["provider_market_ticker"])] = outcome
        outcome_store = postgres_shadow_operations_store(engine)
        for ticker, outcome in outcomes.items():
            fact, _ = outcome_store.record_public_outcome_fact(
                campaign_id=campaign_id,
                plan_id=None,
                market_ticker=ticker,
                raw_payload=(
                    f'{{"market_ticker":"{ticker}","result":"{outcome}","fixture":true}}'
                ).encode(),
                received_at=now - timedelta(days=1),
                endpoint_identity="KALSHI_GET_MARKET",
                response_content_type="application/json",
                resolved_outcome=outcome,
                state=PublicOutcomeState.RESOLVED,
                rule_content_hash=None,
                reason_codes=("FIXTURE_ONLY_FORWARD_OUTCOME",),
            )
            created, skipped = outcome_store.evaluate_public_outcome_fact(
                outcome_fact_id=str(fact["outcome_fact_id"]), evaluated_at=now - timedelta(hours=12)
            )
            # Re-running the local demonstration is safe: the production
            # scoring service returns zero newly-created rows once its
            # immutable evaluation already exists.  A malformed fixture still
            # cannot qualify because the later snapshot requires all 100
            # persisted evaluations.
            if created > 1 or skipped != 0:
                raise RuntimeError("fixture public outcome did not score exactly one prediction")

        review_store = postgres_evidence_review_store(engine)
        fixture_policy = StrategyEvidenceReviewPolicy(
            policy_id="fixture-governed-evidence-paper-v2",
            policy_version="apex-evidence-review-policy-v1",
            supported_strategy_family=None,
            supported_strategy_id=GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID,
            minimum_resolved_predictions=100,
            minimum_distinct_events=30,
            minimum_distinct_markets=30,
            minimum_probability_range_coverage=3,
            maximum_unresolved_integrity_warnings=0,
            maximum_point_brier_difference=Decimal(0),
            maximum_conservative_brier_difference=Decimal(0),
            maximum_point_log_loss_difference=Decimal(0),
            maximum_conservative_log_loss_difference=Decimal(0),
            maximum_recent_degradation=Decimal("0.03"),
            minimum_recent_resolved=20,
            minimum_calibration_coverage=Decimal("0.60"),
            maximum_conflict_frequency=Decimal("0.10"),
            maximum_evidence_age=timedelta(days=90),
            require_configuration_lineage=True,
            require_outcome_lineage=True,
            configuration_hash=ContentHash(
                content_digest({"fixture": "governed-evidence-policy-v1"})
            ),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        review_store.ensure_policy(fixture_policy)
        snapshot, criteria = BuildStrategyEvidenceSnapshot(review_store).build(
            BuildStrategyEvidenceSnapshotCommand(
                strategy_id=descriptor.strategy_id,
                strategy_version=descriptor.version,
                configuration_hash=descriptor.configuration_hash,
                policy_id=fixture_policy.policy_id,
                policy_version=fixture_policy.policy_version,
                cutoff_at=now,
                full_history_started_at=now - timedelta(days=30),
                recent_history_started_at=now - timedelta(days=7),
                idempotency_key="fixture-governed-evidence-snapshot-v1",
            )
        )
        if hard_gate_reasons(criteria):
            raise RuntimeError("qualifying fixture evidence did not pass every hard gate")
        requested = RequestStrategyEvidenceReview(review_store).request(
            RequestStrategyEvidenceReviewCommand(
                strategy_id=descriptor.strategy_id,
                strategy_version=descriptor.version,
                snapshot_id=snapshot.snapshot_id,
                campaign_id=campaign_id,
                expires_at=now + timedelta(hours=4),
                operator_reason="review qualifying fixture-only forward evidence",
                operator_note="all metrics are derived from retained production scoring rows",
                idempotency_key="fixture-governed-evidence-review-v7",
            ),
            now=now,
        )
        review_replay = RequestStrategyEvidenceReview(review_store).request(
            RequestStrategyEvidenceReviewCommand(
                strategy_id=descriptor.strategy_id,
                strategy_version=descriptor.version,
                snapshot_id=snapshot.snapshot_id,
                campaign_id=campaign_id,
                expires_at=now + timedelta(hours=4),
                operator_reason="review qualifying fixture-only forward evidence",
                operator_note="all metrics are derived from retained production scoring rows",
                idempotency_key="fixture-governed-evidence-review-v7",
            ),
            now=now,
        )
        started = StartStrategyEvidenceReview(review_store).start(
            review_id=requested.review_id,
            operator_reason="begin governed review",
            operator_note="fixture-only review",
            idempotency_key="fixture-governed-evidence-review-start-v1",
            now=now,
        )
        limits = PaperTrialLimits(
            paper_execution_policy_id=execution_policy.policy_version_id,
            paper_execution_policy_hash=execution_policy.configuration_hash,
            starts_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(hours=2),
            maximum_paper_cycles=1,
            maximum_approved_opportunities=4,
            maximum_paper_orders=4,
            maximum_simultaneous_open_positions=2,
            maximum_per_opportunity_risk_fraction=RiskFraction("0.20"),
            maximum_strategy_active_risk_fraction=RiskFraction("0.50"),
            maximum_cumulative_authorized_risk=Money("500"),
            maximum_cumulative_realized_paper_loss=Money("0.10"),
            maximum_paper_drawdown=Money("0.10"),
            maximum_capital_lockup_seconds=3600,
            allowed_instrument_class="BOUNDED_PREDICTION_MARKET",
            allowed_provider="FIXTURE",
        )
        approved, trial = DecideStrategyEvidenceReview(review_store).decide(
            DecideStrategyEvidenceReviewCommand(
                review_id=started.review_id,
                decision=StrategyEvidenceReviewState.APPROVED,
                operator_reason="approve bounded fixture PAPER trial",
                operator_note="exact confirmation supplied by local operator",
                idempotency_key="fixture-governed-evidence-review-approve-v1",
                confirmation_text=paper_trial_confirmation_text(
                    strategy_id=descriptor.strategy_id,
                    campaign_id=campaign_id,
                    snapshot_id=snapshot.snapshot_id,
                ),
            ),
            now=now,
            trial_limits=limits,
        )
        if trial is None or approved.state is not StrategyEvidenceReviewState.APPROVED:
            raise RuntimeError("approved evidence review did not create a paper trial")

        autopilot_store.control(
            campaign_id=campaign_id,
            action="PAUSE",
            reason="prove trial path respects campaign pause",
            idempotency_key="fixture-governed-evidence-pause-v1",
            occurred_at=now + timedelta(seconds=1),
        )
        try:
            RunAutopilotPaperCycle(autopilot_store, registry, paper_store, review_store).run(
                PaperCycleCommand(
                    cycle=AutopilotCycleCommand(
                        campaign_id=campaign_id,
                        idempotency_key="fixture-governed-evidence-paused-cycle-v1",
                        maximum_strategies=1,
                        maximum_proposals=2,
                        deadline_at=now + timedelta(seconds=31),
                        operator_note="must be blocked by campaign pause",
                        requested_at=now + timedelta(seconds=1),
                        authority=AutopilotCycleAuthority.PAPER,
                    ),
                    execution_policy=execution_policy,
                )
            )
        except CampaignControlError:
            pause_blocked = True
        else:
            raise RuntimeError("campaign pause did not block a PAPER cycle")
        autopilot_store.control(
            campaign_id=campaign_id,
            action="RESUME",
            reason="resume explicit fixture PAPER trial",
            idempotency_key="fixture-governed-evidence-resume-v1",
            occurred_at=now + timedelta(seconds=2),
        )
        cycle_command = PaperCycleCommand(
            cycle=AutopilotCycleCommand(
                campaign_id=campaign_id,
                idempotency_key="fixture-governed-evidence-cycle-v1",
                maximum_strategies=1,
                maximum_proposals=2,
                deadline_at=now + timedelta(seconds=32),
                operator_note="one governed fixture PAPER cycle",
                requested_at=now + timedelta(seconds=2),
                authority=AutopilotCycleAuthority.PAPER,
            ),
            execution_policy=execution_policy,
        )
        cycle = RunAutopilotPaperCycle(autopilot_store, registry, paper_store, review_store).run(
            cycle_command
        )
        replay = RunAutopilotPaperCycle(autopilot_store, registry, paper_store, review_store).run(
            cycle_command
        )
        conflicting_replay_rejected = False
        try:
            RunAutopilotPaperCycle(autopilot_store, registry, paper_store, review_store).run(
                PaperCycleCommand(
                    cycle=AutopilotCycleCommand(
                        campaign_id=campaign_id,
                        idempotency_key="fixture-governed-evidence-cycle-v1",
                        maximum_strategies=1,
                        maximum_proposals=1,
                        deadline_at=now + timedelta(seconds=32),
                        operator_note="conflicting exact replay",
                        requested_at=now + timedelta(seconds=2),
                        authority=AutopilotCycleAuthority.PAPER,
                    ),
                    execution_policy=execution_policy,
                )
            )
        except AutopilotIdempotencyConflictError:
            conflicting_replay_rejected = True
        with cast(Any, engine).connect() as connection:
            position_rows = tuple(
                connection.execute(
                    position.select()
                    .join(
                        durable_order, durable_order.c.order_id == position.c.originating_order_id
                    )
                    .with_only_columns(
                        position,
                        durable_order.c.proposal_id,
                        durable_order.c.state.label("order_state"),
                    )
                    .where(position.c.campaign_id == campaign_id)
                    .order_by(position.c.position_id)
                ).mappings()
            )
        if len(position_rows) != 2:
            raise RuntimeError("governed fixture cycle did not retain both expected Positions")
        loss_position, retained_position = position_rows
        with cast(Any, engine).connect() as connection:
            from apex.adapters.postgres import opportunity_proposal

            evidence_id = str(
                connection.scalar(
                    paper_execution.select()
                    .with_only_columns(paper_execution.c.evidence_id)
                    .select_from(
                        paper_position_lineage.join(
                            paper_execution,
                            paper_execution.c.paper_execution_id
                            == paper_position_lineage.c.paper_execution_id,
                        )
                    )
                    .where(paper_position_lineage.c.position_id == retained_position["position_id"])
                )
            )

            loss_rule = dict(
                connection.execute(
                    opportunity_proposal.select().where(
                        opportunity_proposal.c.proposal_id == loss_position["proposal_id"]
                    )
                )
                .mappings()
                .one()
            )
            retained_rule = dict(
                connection.execute(
                    opportunity_proposal.select().where(
                        opportunity_proposal.c.proposal_id == retained_position["proposal_id"]
                    )
                )
                .mappings()
                .one()
            )
        loss_settlement = paper_store.settle_position(
            campaign_id=campaign_id,
            position_id=str(loss_position["position_id"]),
            fact=_demo_paper_settlement_fact(
                {**dict(loss_position), **loss_rule},
                classification=SettlementOutcome.NO,
                recorded_at=now + timedelta(hours=1),
            ),
            void_term_content_hash=None,
            recorded_at=now + timedelta(hours=1),
        )
        suspended = review_store.reconcile_trial(
            trial.authorization_id, now=now + timedelta(hours=1, seconds=1)
        )
        if suspended.state is not PaperTrialAuthorizationState.SUSPENDED_LIMIT:
            raise RuntimeError("loss and exhausted cycle limits did not suspend the fixture trial")
        revoked = review_store.transition_trial(
            suspended,
            state=PaperTrialAuthorizationState.REVOKED,
            reason="operator revokes suspended fixture trial",
            idempotency_key="fixture-governed-evidence-revoke-v1",
            now=now + timedelta(hours=1, seconds=2),
        )
        marks_after_revoke = PaperPortfolioOperations(paper_store).mark_positions(
            PaperPositionMarkCommand(
                campaign_id=campaign_id,
                evidence_id=evidence_id,
                recorded_at=now + timedelta(hours=1, seconds=3),
            )
        )
        retained_settlement = paper_store.settle_position(
            campaign_id=campaign_id,
            position_id=str(retained_position["position_id"]),
            fact=_demo_paper_settlement_fact(
                {**dict(retained_position), **retained_rule},
                classification=SettlementOutcome.YES,
                recorded_at=now + timedelta(hours=1, seconds=4),
            ),
            void_term_content_hash=None,
            recorded_at=now + timedelta(hours=1, seconds=4),
        )
        blocked_cycle = RunAutopilotPaperCycle(
            autopilot_store, registry, paper_store, review_store
        ).run(
            PaperCycleCommand(
                cycle=AutopilotCycleCommand(
                    campaign_id=campaign_id,
                    idempotency_key="fixture-governed-evidence-revoked-cycle-v1",
                    maximum_strategies=1,
                    maximum_proposals=2,
                    deadline_at=now + timedelta(hours=1, minutes=1),
                    operator_note="must be blocked by revoked trial",
                    requested_at=now + timedelta(hours=1, seconds=5),
                    authority=AutopilotCycleAuthority.PAPER,
                ),
                execution_policy=execution_policy,
            )
        )
        return {
            "microstructure_review": "BLOCKED_INSUFFICIENT_RESOLVED_FORWARD_EVIDENCE",
            "dynamics_review": "BLOCKED_INSUFFICIENT_RESOLVED_FORWARD_EVIDENCE",
            "fixture_evidence": {
                "predictions": snapshot.total_predictions,
                "resolved": snapshot.resolved_predictions,
                "distinct_events": snapshot.distinct_events,
                "distinct_markets": snapshot.distinct_markets,
                "point_brier_difference": snapshot.point_brier_difference,
                "conservative_brier_difference": snapshot.conservative_brier_difference,
                "point_log_loss_difference": snapshot.point_log_loss_difference,
                "conservative_log_loss_difference": snapshot.conservative_log_loss_difference,
                "probability_range_coverage": snapshot.probability_range_coverage,
            },
            "all_hard_gates_pass": not hard_gate_reasons(criteria),
            "review_request_idempotent": requested.review_id == review_replay.review_id,
            "review_state": approved.state,
            "trial_state_after_revoke": revoked.state,
            "paper_authority": shadow_store.current_authority(descriptor.strategy_id)["authority"],
            "cycle": {"approved": cycle.approved_count, "replay": replay.idempotent_replay},
            "conflicting_replay_rejected": conflicting_replay_rejected,
            "pause_blocked": pause_blocked,
            "suspension_reason": suspended.suspension_reason,
            "revoked_cycle_approved": blocked_cycle.approved_count,
            "settlements": (loss_settlement, retained_settlement),
            "marks_after_revoke": len(marks_after_revoke),
            "provider_order_calls": 0,
            "live_authority": False,
        }
    finally:
        engine.dispose()


def autopilot_control(
    *, campaign_id: str, action: str, reason: str, idempotency_key: str
) -> object:
    store, engine = _autopilot_store()
    try:
        return {
            "campaign_id": campaign_id,
            "operating_state": store.control(
                campaign_id=campaign_id,
                action=action,
                reason=reason,
                idempotency_key=idempotency_key,
                occurred_at=datetime.now(tz=UTC),
            ),
        }
    finally:
        engine.dispose()


def change_strategy_authority(
    *,
    strategy_id: str,
    requested_authority: str,
    operator_reason: str,
    evidence_review_id: str,
    idempotency_key: str,
    confirmation_text: str | None,
) -> object:
    """Apply one audited authority transition; PAPER remains policy-forbidden."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_strategy_shadow_store,
    )
    from apex.application.autopilot import StrategyAuthorityState

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        decision = postgres_strategy_shadow_store(engine).change_authority(
            StrategyAuthorityCommand(
                strategy_id=strategy_id,
                requested_authority=StrategyAuthorityState(requested_authority),
                operator_reason=operator_reason,
                evidence_review_id=evidence_review_id,
                idempotency_key=idempotency_key,
                confirmation_text=confirmation_text,
                requested_at=datetime.now(tz=UTC),
            ),
            operator_action_id=deterministic_id(
                "strategy_authority_operator_action",
                {"strategy_id": strategy_id, "idempotency_key": idempotency_key},
            ),
        )
        return decision
    finally:
        engine.dispose()


def strategy_authority_history(strategy_id: str) -> tuple[dict[str, object], ...]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_strategy_shadow_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        return postgres_strategy_shadow_store(engine).authority_history(strategy_id)
    finally:
        engine.dispose()


def strategy_promotion_readiness(
    *, strategy_id: str, campaign_id: str | None = None
) -> dict[str, object]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_strategy_shadow_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_strategy_shadow_store(engine)
        return store.promotion_readiness(strategy_id=strategy_id, campaign_id=campaign_id)
    finally:
        engine.dispose()


def submit_operator_thesis(
    *,
    campaign_id: str,
    market_snapshot_id: str,
    point_probabilities: tuple[tuple[str, str], ...],
    conservative_probabilities: tuple[tuple[str, str], ...],
    resolution_at: datetime,
    requested_risk_fraction: str,
    thesis_summary: str,
    evidence_references: tuple[str, ...],
    correlation_buckets: tuple[str, ...],
    operator_note: str,
    idempotency_key: str,
) -> dict[str, object]:
    """Run one explicitly new, one-strategy SHADOW cycle for an operator thesis.

    This is the documented cycle model: each thesis creates one fresh bounded
    cycle keyed by the thesis idempotency key.  A replay returns the exact same
    retained decision chain; it never attaches to an ambiguous open cycle.
    """

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_autopilot_store,
        postgres_operator_console_store,
        postgres_strategy_shadow_store,
    )
    from apex.application.autopilot import AutopilotCycleAuthority, StrategyAuthorityState
    from apex.application.operator_console import OperatorActionType

    requested_at = datetime.now(tz=UTC)
    thesis = OperatorThesisInput(
        campaign_id=campaign_id,
        point_probabilities=tuple((outcome, value) for outcome, value in point_probabilities),
        conservative_probabilities=tuple(
            (outcome, value) for outcome, value in conservative_probabilities
        ),
        resolution_at=resolution_at,
        requested_risk_fraction=requested_risk_fraction,
        thesis_summary=thesis_summary,
        evidence_references=evidence_references,
        correlation_buckets=correlation_buckets,
        operator_note=operator_note,
        idempotency_key=idempotency_key,
        requested_at=requested_at,
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        console = postgres_operator_console_store(engine)
        metadata_value = console.campaign_metadata(campaign_id)
        if metadata_value is None:
            raise AutopilotError("campaign is not initialized for the local Operator Console")
        if str(metadata_value["state"]) != "RUNNING":
            raise CampaignControlError("campaign must be RUNNING for a forward shadow thesis")
        shadow_store = postgres_strategy_shadow_store(engine)
        authority = shadow_store.current_authority(OPERATOR_THESIS_STRATEGY_ID)
        if authority["authority"] is not StrategyAuthorityState.SHADOW:
            raise AutopilotError("Operator Thesis is not SHADOW-authorized")
        market = shadow_store.retained_market(market_snapshot_id)
        opportunity = build_operator_thesis_opportunity(market=market, thesis=thesis)
        strategy = OperatorThesisAutopilotStrategy(opportunity, market.snapshot_id)
        command = AutopilotCycleCommand(
            campaign_id=campaign_id,
            idempotency_key=idempotency_key,
            maximum_strategies=1,
            maximum_proposals=1,
            # A thesis cycle is a synchronous local assessment.  Its retained
            # deadline is deliberately tied to the immutable expected
            # resolution rather than a regenerated wall-clock timeout, so an
            # idempotent replay has exactly the original cycle request hash.
            deadline_at=thesis.resolution_at,
            operator_note=operator_note,
            requested_at=requested_at,
            authority=AutopilotCycleAuthority.SHADOW,
        )
        result = RunAutopilotShadowCycle(
            postgres_autopilot_store(engine), StrategyRegistry((strategy,))
        ).run(command)
        invocation_id = deterministic_id(
            "autopilot_invocation",
            {"cycle_id": result.cycle_id, "ordinal": 0, "strategy": strategy.descriptor},
        )
        prediction = build_operator_thesis_prediction(
            market=market,
            thesis=thesis,
            cycle_id=result.cycle_id,
            invocation_id=invocation_id,
            proposal_id=opportunity.proposal.proposal_id if opportunity is not None else None,
        )
        existing_prediction = shadow_store.prediction(prediction.prediction_id)
        if existing_prediction is not None:
            prediction = existing_prediction
            replay = True
        else:
            replay = shadow_store.persist_prediction(prediction)
        action = console.record_action(
            campaign_id=campaign_id,
            action_type=OperatorActionType.SUBMIT_THESIS,
            idempotency_key=idempotency_key,
            operator_note=operator_note,
            target_entity_ids=(prediction.prediction_id, result.cycle_id, market.snapshot_id),
            reason_codes=(
                "SHADOW_THESIS_REPLAY" if replay else "SHADOW_THESIS_RETAINED",
                "NO_FINANCIAL_STATE_CREATED",
            ),
            request={
                "thesis_request_hash": thesis.request_hash,
                "market_snapshot_id": market_snapshot_id,
            },
            result={
                "prediction_id": prediction.prediction_id,
                "cycle": _autopilot_cycle_summary(result),
            },
        )
        return {
            "prediction": prediction,
            "cycle": _autopilot_cycle_summary(result),
            "assessment": result.assessments[0] if result.assessments else None,
            "idempotent_replay": replay
            or result.idempotent_replay
            or bool(action["idempotent_replay"]),
        }
    finally:
        engine.dispose()


def list_shadow_predictions(
    *, campaign_id: str | None = None, strategy_id: str | None = None, resolved: bool | None = None
) -> tuple[dict[str, object], ...]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_strategy_shadow_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        return postgres_strategy_shadow_store(engine).predictions(
            campaign_id=campaign_id,
            strategy_id=strategy_id,
            state=(
                ShadowPredictionState.RESOLVED
                if resolved
                else ShadowPredictionState.UNRESOLVED
                if resolved is False
                else None
            ),
        )
    finally:
        engine.dispose()


def shadow_prediction_detail(prediction_id: str) -> object:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_strategy_shadow_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        value = postgres_strategy_shadow_store(engine).prediction(prediction_id)
        if value is None:
            raise AutopilotError("shadow prediction was not found")
        return value
    finally:
        engine.dispose()


def evaluate_shadow_prediction(
    *, prediction_id: str, settlement_fact_id_value: str, operator_note: str, idempotency_key: str
) -> dict[str, object]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_operator_console_store,
        postgres_strategy_shadow_store,
    )
    from apex.application.operator_console import OperatorActionType

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_strategy_shadow_store(engine)
        prediction = store.prediction(prediction_id)
        if prediction is None or prediction.campaign_id is None:
            raise AutopilotError("shadow prediction was not found")
        evaluation, replay = store.evaluate_prediction(
            prediction_id=prediction_id,
            settlement_fact_id_value=settlement_fact_id_value,
            evaluated_at=datetime.now(tz=UTC),
        )
        action = postgres_operator_console_store(engine).record_action(
            campaign_id=prediction.campaign_id,
            action_type=OperatorActionType.EVALUATE_PREDICTION,
            idempotency_key=idempotency_key,
            operator_note=operator_note,
            target_entity_ids=(prediction_id, settlement_fact_id_value, evaluation.evaluation_id),
            reason_codes=("SETTLEMENT_FACT_ONLY_EVALUATION", "NO_FINANCIAL_STATE_CREATED"),
            request={
                "prediction_id": prediction_id,
                "settlement_fact_id": settlement_fact_id_value,
            },
            result=evaluation,
        )
        return {
            "evaluation": evaluation,
            "idempotent_replay": replay or bool(action["idempotent_replay"]),
        }
    finally:
        engine.dispose()


def live_shadow_session(
    *,
    campaign_id: str,
    strategy_id: str,
    iterations: int,
    interval_seconds: int,
    market_limit: int,
    maximum_pages: int = 1,
    maximum_enrichments: int = 20,
    observations_per_market: int = 3,
    orderbook_depth: int = 5,
) -> dict[str, object]:
    """Run one foreground, sequential, bounded public-data shadow session."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_autopilot_store,
        postgres_operator_console_store,
        postgres_public_binary_complement_store,
        postgres_public_market_discovery_store,
        postgres_strategy_shadow_store,
    )
    from apex.application.autopilot import StrategyAuthorityState
    from apex.application.market_dynamics_shadow import (
        MarketDynamicsAutopilotStrategy,
        RunMarketDynamicsShadowStrategy,
        evaluate_retained_market_dynamics,
    )
    from apex.application.operator_console import OperatorActionType
    from apex.application.orderbook_microstructure_shadow import (
        OrderbookMicrostructureAutopilotStrategy,
        RunOrderbookMicrostructureShadowStrategy,
        evaluate_retained_microstructure,
    )
    from apex.domain.market_dynamics import (
        MarketDynamicsStrategyConfig,
        evaluate_market_dynamics_features,
    )
    from apex.domain.orderbook_microstructure import (
        OrderbookMicrostructureStrategyConfig,
        evaluate_microstructure_features,
    )

    if not 1 <= iterations <= 20:
        raise ValueError("live shadow iterations must be between 1 and 20")
    if not 10 <= interval_seconds <= 3_600:
        raise ValueError("live shadow interval must be between 10 and 3600 seconds")
    if not 1 <= market_limit <= 100:
        raise ValueError("live shadow market limit must be between 1 and 100")
    if not 1 <= maximum_pages <= 3 or not 1 <= maximum_enrichments <= 100:
        raise ValueError("live shadow public selection bounds are invalid")
    if not 2 <= observations_per_market <= 20 or not 1 <= orderbook_depth <= 20:
        raise ValueError("live shadow observation and depth bounds are invalid")
    if strategy_id not in (
        MARKET_CONSENSUS_BASELINE_STRATEGY_ID,
        OPERATOR_THESIS_STRATEGY_ID,
        "kalshi-orderbook-microstructure-v1",
        "kalshi-market-dynamics-v1",
    ):
        raise ValueError("live shadow session supports only governed production SHADOW strategies")
    if strategy_id == "kalshi-market-dynamics-v1" and iterations < 2:
        raise ValueError("market dynamics live shadow sessions require at least two iterations")
    engine = create_postgres_engine(PostgresSettings.from_env())
    started_at = datetime.now(tz=UTC)
    try:
        console = postgres_operator_console_store(engine)
        metadata_value = console.campaign_metadata(campaign_id)
        if metadata_value is None or str(metadata_value["state"]) != "RUNNING":
            raise CampaignControlError("campaign must be RUNNING for a live shadow session")
        shadow_store = postgres_strategy_shadow_store(engine)
        authority = shadow_store.current_authority(strategy_id)
        if authority["authority"] is not StrategyAuthorityState.SHADOW:
            raise AutopilotError("strategy is not SHADOW-authorized")
        session_id = shadow_store.start_observation_session(
            campaign_id=campaign_id,
            strategy_id=strategy_id,
            strategy_version=str(authority["strategy_version"]),
            iterations=iterations,
            interval_seconds=interval_seconds,
            market_limit=market_limit,
            started_at=started_at,
        )
        discovery_store = postgres_public_market_discovery_store(engine)
        completed = 0
        failed = 0
        last_error: str | None = None
        total_predictions = 0
        for ordinal in range(iterations):
            iteration_started = datetime.now(tz=UTC)
            print(f"live-shadow iteration {ordinal + 1}/{iterations} starting")
            try:
                if strategy_id == MARKET_CONSENSUS_BASELINE_STRATEGY_ID:
                    summary = DiscoverPublicPredictionMarkets(
                        client=KalshiPublicMarketDataClient(),
                        normalizer=KalshiPublicMarketNormalizer(),
                        store=discovery_store,
                        clock=lambda: datetime.now(tz=UTC),
                    ).discover(
                        PublicMarketScanRequest(
                            run_key=deterministic_id(
                                "live_shadow_market_discovery",
                                {"session_id": session_id, "ordinal": ordinal},
                            ),
                            page_size=market_limit,
                            maximum_pages=maximum_pages,
                            timeout_seconds=10,
                        )
                    )
                    snapshot_ids = discovery_store.market_snapshot_ids_for_run(
                        summary.ingestion_run_id, limit=market_limit
                    )
                    for snapshot_id in snapshot_ids:
                        market = shadow_store.retained_market(snapshot_id)
                        baseline = build_market_consensus_baseline_prediction(
                            market=market,
                            campaign_id=campaign_id,
                            decision_at=datetime.now(tz=UTC),
                        )
                        shadow_store.persist_prediction(baseline)
                    prediction_count = len(snapshot_ids)
                    run_id: str | None = summary.ingestion_run_id
                    reasons = ("BASELINE_PREDICTIONS_RETAINED", "NO_OPPORTUNITIES_CREATED")
                elif strategy_id == "kalshi-orderbook-microstructure-v1":
                    summary = DiscoverPublicPredictionMarkets(
                        client=KalshiPublicMarketDataClient(),
                        normalizer=KalshiPublicMarketNormalizer(),
                        store=discovery_store,
                        clock=lambda: datetime.now(tz=UTC),
                    ).discover(
                        PublicMarketScanRequest(
                            run_key=deterministic_id(
                                "live_shadow_microstructure_discovery",
                                {"session_id": session_id, "ordinal": ordinal},
                            ),
                            page_size=market_limit,
                            maximum_pages=maximum_pages,
                            timeout_seconds=10,
                        )
                    )
                    # This uses the existing bounded GET-only orderbook and fee
                    # collector. Requests are sequential; one completed public
                    # scan is retained before the feature evaluator reads it.
                    ScanPublicBinaryComplements(
                        client=KalshiPublicOrderbookDataClient(),
                        store=postgres_public_binary_complement_store(engine),
                        clock=lambda: datetime.now(tz=UTC),
                    ).scan(
                        BinaryComplementScanRequest(
                            market_discovery_run_id=summary.ingestion_run_id,
                            market_limit=market_limit,
                            maximum_depth=orderbook_depth,
                            maximum_bundle_quantity=Decimal("10"),
                            timeout_seconds=10,
                        )
                    )
                    snapshot_ids = discovery_store.market_snapshot_ids_for_run(
                        summary.ingestion_run_id, limit=market_limit
                    )
                    config = OrderbookMicrostructureStrategyConfig(
                        maximum_markets_per_invocation=market_limit,
                        orderbook_depth=orderbook_depth,
                        minimum_observations_per_market=observations_per_market,
                    )
                    decision_at = datetime.now(tz=UTC)
                    prepared: list[tuple[object, object, object, object]] = []
                    preliminary = []
                    for snapshot_id in snapshot_ids[:maximum_enrichments]:
                        market = shadow_store.retained_market(snapshot_id)
                        evidence = shadow_store.retained_microstructure_market(
                            market_snapshot_id=snapshot_id,
                            maximum_observations=observations_per_market,
                        )
                        feature = evaluate_microstructure_features(
                            market=evidence, configuration=config, decision_at=decision_at
                        )
                        evaluation = evaluate_retained_microstructure(
                            campaign_id=campaign_id,
                            market=market,
                            feature=feature,
                            configuration=config,
                            decision_at=decision_at,
                            fee_evidence=(
                                evidence.observations[-1].fee if evidence.observations else None
                            ),
                        )
                        prepared.append((market, evidence, feature, evaluation))
                        preliminary.append(evaluation)
                    strategy = OrderbookMicrostructureAutopilotStrategy(tuple(preliminary))
                    cycle = RunAutopilotShadowCycle(
                        postgres_autopilot_store(engine), StrategyRegistry((strategy,))
                    ).run(
                        AutopilotCycleCommand(
                            campaign_id=campaign_id,
                            idempotency_key=deterministic_id(
                                "live_shadow_microstructure_cycle",
                                {"session_id": session_id, "ordinal": ordinal},
                            ),
                            maximum_strategies=1,
                            maximum_proposals=market_limit,
                            deadline_at=decision_at + timedelta(seconds=30),
                            operator_note="bounded public orderbook microstructure SHADOW",
                            requested_at=decision_at,
                            authority=AutopilotCycleAuthority.SHADOW,
                        )
                    )
                    invocation_id = deterministic_id(
                        "autopilot_invocation",
                        {"cycle_id": cycle.cycle_id, "ordinal": 0, "strategy": strategy.descriptor},
                    )
                    writer = RunOrderbookMicrostructureShadowStrategy(shadow_store)
                    completed_evaluations = tuple(
                        writer.run(
                            campaign_id=campaign_id,
                            market=cast(Any, market),
                            feature=cast(Any, feature),
                            configuration=config,
                            decision_at=decision_at,
                            fee_evidence=(
                                cast(Any, evidence).observations[-1].fee
                                if cast(Any, evidence).observations
                                else None
                            ),
                            cycle_id=cycle.cycle_id,
                            invocation_id=invocation_id,
                        )
                        for market, evidence, feature, _evaluation in prepared
                    )
                    prediction_count = sum(
                        item.prediction is not None for item in completed_evaluations
                    )
                    run_id = summary.ingestion_run_id
                    reasons = (
                        "MICROSTRUCTURE_FEATURES_RETAINED",
                        "SHADOW_AUTOPILOT_ASSESSMENT_RETAINED",
                    )
                elif strategy_id == "kalshi-market-dynamics-v1":
                    summary = DiscoverPublicPredictionMarkets(
                        client=KalshiPublicMarketDataClient(),
                        normalizer=KalshiPublicMarketNormalizer(),
                        store=discovery_store,
                        clock=lambda: datetime.now(tz=UTC),
                    ).discover(
                        PublicMarketScanRequest(
                            run_key=deterministic_id(
                                "live_shadow_market_dynamics_discovery",
                                {"session_id": session_id, "ordinal": ordinal},
                            ),
                            page_size=market_limit,
                            maximum_pages=maximum_pages,
                            timeout_seconds=10,
                        )
                    )
                    snapshot_ids = discovery_store.market_snapshot_ids_for_run(
                        summary.ingestion_run_id, limit=market_limit
                    )
                    dynamics_config = MarketDynamicsStrategyConfig(
                        maximum_markets_per_invocation=market_limit,
                        maximum_observations=max(observations_per_market, 3),
                        minimum_observations=3,
                    )
                    decision_at = datetime.now(tz=UTC)
                    dynamics_prepared: list[tuple[object, object, object]] = []
                    dynamics_preliminary = []
                    for snapshot_id in snapshot_ids[:maximum_enrichments]:
                        dynamics_market = shadow_store.retained_market(snapshot_id)
                        dynamics_evidence = shadow_store.retained_market_dynamics_market(
                            market_snapshot_id=snapshot_id,
                            maximum_observations=dynamics_config.maximum_observations,
                        )
                        dynamics_feature = evaluate_market_dynamics_features(
                            market=dynamics_evidence,
                            configuration=dynamics_config,
                            decision_at=decision_at,
                        )
                        dynamics_evaluation = evaluate_retained_market_dynamics(
                            campaign_id=campaign_id,
                            market=dynamics_market,
                            feature=dynamics_feature,
                            configuration=dynamics_config,
                            decision_at=decision_at,
                        )
                        dynamics_prepared.append(
                            (dynamics_market, dynamics_feature, dynamics_evaluation)
                        )
                        dynamics_preliminary.append(dynamics_evaluation)
                    dynamics_strategy = MarketDynamicsAutopilotStrategy(tuple(dynamics_preliminary))
                    cycle = RunAutopilotShadowCycle(
                        postgres_autopilot_store(engine), StrategyRegistry((dynamics_strategy,))
                    ).run(
                        AutopilotCycleCommand(
                            campaign_id=campaign_id,
                            idempotency_key=deterministic_id(
                                "live_shadow_market_dynamics_cycle",
                                {"session_id": session_id, "ordinal": ordinal},
                            ),
                            maximum_strategies=1,
                            maximum_proposals=market_limit,
                            deadline_at=decision_at + timedelta(seconds=30),
                            operator_note="bounded public market dynamics SHADOW",
                            requested_at=decision_at,
                            authority=AutopilotCycleAuthority.SHADOW,
                        )
                    )
                    invocation_id = deterministic_id(
                        "autopilot_invocation",
                        {
                            "cycle_id": cycle.cycle_id,
                            "ordinal": 0,
                            "strategy": dynamics_strategy.descriptor,
                        },
                    )
                    dynamics_writer = RunMarketDynamicsShadowStrategy(shadow_store)
                    dynamics_completed_evaluations = tuple(
                        dynamics_writer.run(
                            campaign_id=campaign_id,
                            market=cast(Any, market),
                            feature=cast(Any, feature),
                            configuration=dynamics_config,
                            decision_at=decision_at,
                            cycle_id=cycle.cycle_id,
                            invocation_id=invocation_id,
                        )
                        for market, feature, _evaluation in dynamics_prepared
                    )
                    prediction_count = sum(
                        item.prediction is not None for item in dynamics_completed_evaluations
                    )
                    run_id = summary.ingestion_run_id
                    reasons = (
                        "MARKET_DYNAMICS_FEATURES_RETAINED",
                        "SHADOW_AUTOPILOT_ASSESSMENT_RETAINED",
                    )
                else:
                    # An operator thesis is immutable; the session may refresh
                    # public evidence but never changes its forecast values.
                    unresolved = shadow_store.predictions(
                        campaign_id=campaign_id,
                        strategy_id=OPERATOR_THESIS_STRATEGY_ID,
                        state=ShadowPredictionState.UNRESOLVED,
                        limit=market_limit,
                    )
                    prediction_count = len(unresolved)
                    run_id = None
                    reasons = ("OPERATOR_THESIS_VALUES_UNCHANGED", "NO_OPPORTUNITIES_CREATED")
                completed_at = datetime.now(tz=UTC)
                shadow_store.record_observation_iteration(
                    session_id=session_id,
                    ordinal=ordinal,
                    started_at=iteration_started,
                    completed_at=completed_at,
                    ingestion_run_id=run_id,
                    state="COMPLETED",
                    prediction_count=prediction_count,
                    reason_codes=reasons,
                )
                total_predictions += prediction_count
                completed += 1
                print(f"live-shadow iteration {ordinal + 1}/{iterations} completed")
            except Exception as error:
                failed += 1
                last_error = type(error).__name__
                shadow_store.record_observation_iteration(
                    session_id=session_id,
                    ordinal=ordinal,
                    started_at=iteration_started,
                    completed_at=datetime.now(tz=UTC),
                    ingestion_run_id=None,
                    state="FAILED",
                    prediction_count=0,
                    reason_codes=("PUBLIC_SHADOW_ITERATION_FAILED",),
                )
                print(f"live-shadow iteration {ordinal + 1}/{iterations} failed: {last_error}")
                break
            if ordinal + 1 < iterations:
                time.sleep(interval_seconds)
        # A partial session records the completed requests and the terminal
        # failure; remaining bounds are intentionally not retried or restarted.
        state = "COMPLETED" if completed == iterations else "PARTIAL"
        shadow_store.complete_observation_session(
            session_id=session_id,
            completed_at=datetime.now(tz=UTC),
            completed_iterations=completed,
            failed_iterations=failed,
            state=state,
            last_error_code=last_error,
        )
        console.record_action(
            campaign_id=campaign_id,
            action_type=OperatorActionType.RUN_LIVE_SHADOW_SESSION,
            idempotency_key=session_id,
            operator_note="bounded foreground live shadow session",
            target_entity_ids=(session_id,),
            reason_codes=("PUBLIC_GET_ONLY", "NO_FINANCIAL_STATE_CREATED"),
            request={
                "strategy_id": strategy_id,
                "iterations": iterations,
                "market_limit": market_limit,
                "maximum_pages": maximum_pages,
                "maximum_enrichments": maximum_enrichments,
                "observations_per_market": observations_per_market,
                "orderbook_depth": orderbook_depth,
            },
            result={"completed_iterations": completed, "failed_iterations": failed},
        )
        return {
            "session_id": session_id,
            "state": state,
            "completed_iterations": completed,
            "failed_iterations": failed,
            "prediction_count": total_predictions,
            "last_error_code": last_error,
        }
    finally:
        engine.dispose()


def multi_strategy_shadow_cycle(
    *,
    campaign_id: str,
    idempotency_key: str,
    market_limit: int,
    maximum_predictions: int,
    maximum_opportunities: int,
    cohort_candidates: tuple[object, ...] | None = None,
    cohort_configuration: object | None = None,
    strategy_configurations: dict[str, object] | None = None,
) -> dict[str, object]:
    """Evaluate the three existing strategies from retained evidence only."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_multi_strategy_shadow_store,
        postgres_strategy_shadow_store,
    )
    from apex.application.market_dynamics_shadow import (
        RunMarketDynamicsShadowStrategy,
        market_dynamics_strategy_descriptor,
    )
    from apex.application.multi_strategy_shadow import (
        BuildEnrichedShadowMarketCohort,
        MultiStrategyInvocation,
        MultiStrategyShadowCycleCommand,
        RunMultiStrategyShadowCampaignCycle,
    )
    from apex.application.orderbook_microstructure_shadow import (
        RunOrderbookMicrostructureShadowStrategy,
        orderbook_microstructure_strategy_descriptor,
    )
    from apex.application.strategy_shadow import (
        build_market_consensus_baseline_prediction,
        production_shadow_strategy_descriptors,
    )
    from apex.domain.market_dynamics import (
        MarketDynamicsStrategyConfig,
        evaluate_market_dynamics_features,
    )
    from apex.domain.multi_strategy import ShadowMarketCohortConfiguration
    from apex.domain.orderbook_microstructure import (
        OrderbookMicrostructureStrategyConfig,
        evaluate_microstructure_features,
    )

    if not 3 <= market_limit <= 50:
        raise ValueError("multi-strategy market limit must be between three and 50")
    now = datetime.now(tz=UTC)
    configuration = (
        cast(ShadowMarketCohortConfiguration, cohort_configuration)
        if cohort_configuration is not None
        else ShadowMarketCohortConfiguration(selected_market_count=market_limit)
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        store = postgres_multi_strategy_shadow_store(engine)
        shadow_store = postgres_strategy_shadow_store(engine)
        candidates = (
            cast(tuple[Any, ...], cohort_candidates)
            if cohort_candidates is not None
            else store.latest_enriched_markets(decision_at=now)
        )
        cohort = BuildEnrichedShadowMarketCohort.build(
            campaign_id=campaign_id,
            cycle_key=idempotency_key,
            configuration=configuration,
            candidates=candidates,
            decision_at=now,
        )
        descriptors = {item.strategy_id: item for item in production_shadow_strategy_descriptors()}
        consensus_predictions = []
        micro_predictions = []
        micro_opportunities = []
        micro_features = []
        micro_prediction_features = []
        dynamics_predictions = []
        dynamics_opportunities = []
        dynamics_features = []
        dynamics_prediction_features = []
        micro_reasons: set[str] = set()
        dynamics_reasons: set[str] = set()
        if strategy_configurations is None:
            micro_config = OrderbookMicrostructureStrategyConfig(
                maximum_markets_per_invocation=market_limit,
                minimum_observations_per_market=configuration.required_observations,
            )
            dynamics_config = MarketDynamicsStrategyConfig(
                maximum_markets_per_invocation=market_limit,
                minimum_observations=configuration.required_observations,
                maximum_observations=configuration.required_observations,
                maximum_observation_age=configuration.maximum_observation_age,
            )
        else:
            bound_micro = strategy_configurations.get("kalshi-orderbook-microstructure-v1")
            bound_dynamics = strategy_configurations.get("kalshi-market-dynamics-v1")
            if not isinstance(bound_micro, OrderbookMicrostructureStrategyConfig) or not isinstance(
                bound_dynamics, MarketDynamicsStrategyConfig
            ):
                raise ValueError("PLAN_CONFIGURATION_LINEAGE_MISSING")
            micro_config = bound_micro
            dynamics_config = bound_dynamics
            if (
                micro_config.maximum_markets_per_invocation != market_limit
                or dynamics_config.maximum_markets_per_invocation != market_limit
            ):
                raise ValueError("PLAN_CONFIGURATION_MISMATCH")
        micro_writer = RunOrderbookMicrostructureShadowStrategy(shadow_store)
        dynamics_writer = RunMarketDynamicsShadowStrategy(shadow_store)
        for member in cohort.selected:
            market = shadow_store.retained_market(member.market_snapshot_id)
            consensus = build_market_consensus_baseline_prediction(
                market=market, campaign_id=campaign_id, decision_at=now
            )
            shadow_store.persist_prediction(consensus)
            consensus_predictions.append(consensus)
            try:
                micro_evidence = shadow_store.retained_microstructure_market(
                    market_snapshot_id=member.market_snapshot_id,
                    maximum_observations=configuration.required_observations,
                )
                micro_feature = evaluate_microstructure_features(
                    market=micro_evidence, configuration=micro_config, decision_at=now
                )
                micro_evaluation = micro_writer.run(
                    campaign_id=campaign_id,
                    market=market,
                    feature=micro_feature,
                    configuration=micro_config,
                    decision_at=now,
                    fee_evidence=(
                        micro_evidence.observations[-1].fee if micro_evidence.observations else None
                    ),
                    expected_configuration_hash=orderbook_microstructure_strategy_descriptor(
                        micro_config
                    ).configuration_hash,
                    expected_configuration_version=micro_config.configuration_version,
                )
                micro_features.append(micro_feature.feature_snapshot_id)
                micro_reasons.update(micro_evaluation.reason_codes)
                if micro_evaluation.prediction is not None:
                    micro_predictions.append(micro_evaluation.prediction)
                    micro_prediction_features.append(
                        (
                            micro_evaluation.prediction.prediction_id,
                            micro_feature.feature_snapshot_id,
                        )
                    )
                if micro_evaluation.opportunity is not None:
                    micro_opportunities.append(micro_evaluation.opportunity)
            except Exception as error:
                micro_reasons.add("MICROSTRUCTURE_MARKET_EVALUATION_FAILED_" + type(error).__name__)
            try:
                dynamics_evidence = shadow_store.retained_market_dynamics_market(
                    market_snapshot_id=member.market_snapshot_id,
                    maximum_observations=configuration.required_observations,
                )
                dynamics_feature = evaluate_market_dynamics_features(
                    market=dynamics_evidence, configuration=dynamics_config, decision_at=now
                )
                dynamics_evaluation = dynamics_writer.run(
                    campaign_id=campaign_id,
                    market=market,
                    feature=dynamics_feature,
                    configuration=dynamics_config,
                    decision_at=now,
                    expected_configuration_hash=market_dynamics_strategy_descriptor(
                        dynamics_config
                    ).configuration_hash,
                    expected_configuration_version=dynamics_config.configuration_version,
                )
                dynamics_features.append(dynamics_feature.feature_snapshot_id)
                dynamics_reasons.update(dynamics_evaluation.reason_codes)
                if dynamics_evaluation.prediction is not None:
                    dynamics_predictions.append(dynamics_evaluation.prediction)
                    dynamics_prediction_features.append(
                        (
                            dynamics_evaluation.prediction.prediction_id,
                            dynamics_feature.feature_snapshot_id,
                        )
                    )
                if dynamics_evaluation.opportunity is not None:
                    dynamics_opportunities.append(dynamics_evaluation.opportunity)
            except Exception as error:
                dynamics_reasons.add(
                    "MARKET_DYNAMICS_MARKET_EVALUATION_FAILED_" + type(error).__name__
                )
        invocations = (
            MultiStrategyInvocation(
                descriptor=descriptors["market-consensus-baseline-v1"],
                predictions=tuple(consensus_predictions),
                opportunities=(),
                feature_snapshot_ids=(),
                reason_codes=("RETAINED_MARKET_CONSENSUS_PREDICTIONS",),
            ),
            MultiStrategyInvocation(
                descriptor=orderbook_microstructure_strategy_descriptor(micro_config),
                predictions=tuple(micro_predictions),
                opportunities=tuple(micro_opportunities),
                feature_snapshot_ids=tuple(sorted(micro_features)),
                reason_codes=tuple(sorted(micro_reasons or {"NO_MICROSTRUCTURE_PREDICTIONS"})),
                prediction_feature_snapshot_ids=tuple(sorted(micro_prediction_features)),
            ),
            MultiStrategyInvocation(
                descriptor=market_dynamics_strategy_descriptor(dynamics_config),
                predictions=tuple(dynamics_predictions),
                opportunities=tuple(dynamics_opportunities),
                feature_snapshot_ids=tuple(sorted(dynamics_features)),
                reason_codes=tuple(sorted(dynamics_reasons or {"NO_DYNAMICS_PREDICTIONS"})),
                prediction_feature_snapshot_ids=tuple(sorted(dynamics_prediction_features)),
            ),
        )
        command = MultiStrategyShadowCycleCommand(
            campaign_id=campaign_id,
            idempotency_key=idempotency_key,
            cohort_configuration=configuration,
            strategy_configuration_hashes=tuple(
                sorted(
                    (
                        (
                            descriptors["market-consensus-baseline-v1"].strategy_id,
                            descriptors["market-consensus-baseline-v1"].configuration_hash.value,
                        ),
                        (
                            invocations[1].descriptor.strategy_id,
                            invocations[1].descriptor.configuration_hash.value,
                        ),
                        (
                            invocations[2].descriptor.strategy_id,
                            invocations[2].descriptor.configuration_hash.value,
                        ),
                    )
                )
            ),
            maximum_predictions=maximum_predictions,
            maximum_opportunities=maximum_opportunities,
            deadline_at=now + timedelta(seconds=30),
            operator_note="bounded multi-strategy SHADOW / HYPOTHETICAL evaluation",
            requested_at=now,
        )
        result = RunMultiStrategyShadowCampaignCycle(store).run(
            command=command, cohort=cohort, invocations=invocations
        )
        return {
            "cycle_id": result.cycle_id,
            "state": result.state,
            "idempotent_replay": result.idempotent_replay,
            "selected_markets": tuple(item.market_ticker for item in result.cohort.selected),
            "invocation_count": result.invocation_count,
            "prediction_count": result.prediction_count,
            "opportunity_count": result.opportunity_count,
            "comparison_count": len(result.comparisons),
            "conflict_count": len(result.conflicts),
            "hypothetical_admission_count": sum(
                item.state.value == "ADMITTED" for item in result.allocations
            ),
            "hypothetical_rejection_count": sum(
                item.state.value == "REJECTED" for item in result.allocations
            ),
            "requested_risk_total": sum(
                (item.requested_risk for item in result.allocations), Decimal(0)
            ),
            "allocated_risk_total": sum(
                (item.allocated_risk for item in result.allocations), Decimal(0)
            ),
            "remaining_shadow_capacity": result.remaining_shadow_capacity,
            "reason_codes": result.reason_codes,
            "financial_authority": "NONE",
        }
    finally:
        engine.dispose()


def multi_strategy_shadow_session(
    *,
    campaign_id: str,
    iterations: int,
    interval_seconds: int,
    market_limit: int,
    maximum_pages: int,
    maximum_enrichments: int,
    orderbook_depth: int,
) -> dict[str, object]:
    """Run one bounded foreground session from exact shared market evidence."""

    if not 3 <= iterations <= 20:
        raise ValueError("multi-strategy iterations must be between three and 20")
    if not 10 <= interval_seconds <= 3600:
        raise ValueError("multi-strategy interval must be between 10 and 3600 seconds")
    if not 3 <= market_limit <= 50:
        raise ValueError("multi-strategy market limit must be between three and 50")
    if not 1 <= maximum_pages <= 5:
        raise ValueError("multi-strategy maximum pages must be between one and five")
    if not 3 <= maximum_enrichments <= 80:
        raise ValueError("multi-strategy enrichments must be between three and 80")
    if not 1 <= orderbook_depth <= 20:
        raise ValueError("multi-strategy orderbook depth must be between one and 20")
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_multi_strategy_shadow_store,
        postgres_public_market_discovery_store,
    )
    from apex.application.capture_public_orderbook_session import (
        PublicOrderbookCaptureTerminalError,
    )
    from apex.application.multi_strategy_shadow import BuildEnrichedShadowMarketCohort
    from apex.domain.multi_strategy import EnrichedShadowMarket, ShadowMarketCohortConfiguration

    engine = create_postgres_engine(PostgresSettings.from_env())
    session_key = deterministic_id(
        "multi_strategy_shadow_session",
        {
            "campaign_id": campaign_id,
            "started_at": datetime.now(tz=UTC),
            "iterations": iterations,
            "market_limit": market_limit,
        },
    )
    completed = 0
    failed = 0
    cycle_ids: list[str] = []
    selected_markets: tuple[str, ...] = ()
    last_completed_stage: str | None = None
    interruption_reason: str | None = None
    try:
        discovery_store = postgres_public_market_discovery_store(engine)
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        multi_store = postgres_multi_strategy_shadow_store(engine)
        capture = CaptureBoundedPublicOrderbookSession(
            client=KalshiPublicOrderbookDataClient(),
            store=capture_store,
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            normalizer=KalshiPublicMarketNormalizer(),
        )
        event_normalizer = KalshiPublicEventNormalizer()
        configuration = ShadowMarketCohortConfiguration(
            maximum_pages=maximum_pages,
            maximum_inspected_markets=max(20, market_limit * maximum_pages),
            maximum_enrichments=maximum_enrichments,
            selected_market_count=market_limit,
        )
        multi_store.start_multi_strategy_session(
            session_id=session_key,
            campaign_id=campaign_id,
            iterations=iterations,
            interval_seconds=interval_seconds,
            market_limit=market_limit,
            maximum_pages=maximum_pages,
            maximum_enrichments=maximum_enrichments,
            orderbook_depth=orderbook_depth,
            started_at=datetime.now(tz=UTC),
        )
        for ordinal in range(iterations):
            started = datetime.now(tz=UTC)
            stage = "MARKET_DISCOVERY_COMPLETED"
            try:
                summary = DiscoverPublicPredictionMarkets(
                    client=KalshiPublicMarketDataClient(),
                    normalizer=KalshiPublicMarketNormalizer(),
                    store=discovery_store,
                    clock=lambda: datetime.now(tz=UTC),
                ).discover(
                    PublicMarketScanRequest(
                        run_key=deterministic_id(
                            "multi_strategy_shadow_market_discovery",
                            {"session_id": session_key, "ordinal": ordinal},
                        ),
                        page_size=min(100, market_limit),
                        maximum_pages=maximum_pages,
                        timeout_seconds=10,
                    )
                )
                candidates = capture_store.select_capture_candidates(
                    ingestion_run_id=summary.ingestion_run_id,
                    limit=maximum_enrichments,
                )
                stage = "CANDIDATE_RANKING_COMPLETED"
                refresh_by_snapshot: dict[str, Any] = {}

                def enrich_candidate(
                    candidate: object,
                    *,
                    iteration_ordinal: int = ordinal,
                    refreshes: dict[str, Any] = refresh_by_snapshot,
                ) -> tuple[EnrichedShadowMarket | None, tuple[str, ...]]:
                    if not isinstance(candidate, CaptureMarketCandidate):
                        return None, ("UNSUPPORTED_MARKET_STRUCTURE",)
                    request = BoundedOrderbookCaptureRequest(
                        session_id=deterministic_id(
                            "multi_strategy_shared_enrichment",
                            {
                                "session_id": session_key,
                                "iteration": iteration_ordinal,
                                "market_ticker": candidate.provider_market_ticker,
                            },
                        ),
                        market_ticker=candidate.provider_market_ticker,
                        observations=3,
                        interval_seconds=10,
                        maximum_depth=orderbook_depth,
                        maximum_bundle_quantity=Decimal("10"),
                        timeout_seconds=10,
                    )
                    attempt = capture.enrich_candidate(request=request, candidate=candidate)
                    refresh = attempt.selected
                    if refresh is None:
                        return None, attempt.reason_codes
                    try:
                        event_raw = next(
                            raw
                            for raw in refresh.raw_responses
                            if isinstance(raw.response.document.get("event"), dict)
                        )
                        event = event_raw.response.document["event"]
                        assert isinstance(event, dict)
                        event_page = CapturedPublicEventPage(
                            endpoint_identity=event_raw.response.endpoint_identity,
                            request_cursor=None,
                            received_at=event_raw.response.received_at,
                            http_status=event_raw.response.http_status,
                            response_content_type=event_raw.response.response_content_type,
                            raw_payload=event_raw.response.raw_payload,
                            response_cursor=None,
                            document={"events": [event]},
                        )
                        snapshots = event_normalizer.normalize_events(
                            raw_response_id=event_raw.raw_response_id,
                            page=event_page,
                        )
                        if len(snapshots) != 1:
                            return None, ("EVENT_TICKER_MISMATCH",)
                        event_snapshot = snapshots[0]
                        relationships = link_event_markets(event_snapshot, (refresh.snapshot,))
                        if (
                            event_snapshot.provider_event_ticker
                            != refresh.market.provider_event_ticker
                            or event_snapshot.series_ticker != refresh.market.series_ticker
                            or event_snapshot.data_quality_state.value != "ELIGIBLE"
                            or len(relationships) != 1
                            or relationships[0].relationship_state.value != "EXACT"
                        ):
                            return None, ("EVENT_MARKET_LINEAGE_MISMATCH",)
                        evaluation = evaluate_event_consistency(
                            event_snapshot,
                            relationships=relationships,
                            markets=(refresh.snapshot,),
                            evaluated_at=refresh.response.received_at,
                            evaluator_version="public-event-consistency-v1",
                        )
                        enriched = multi_store.persist_shared_enriched_market(
                            session_id=session_key,
                            iteration_ordinal=iteration_ordinal,
                            list_market_snapshot_id=candidate.market_snapshot_id,
                            refresh=refresh,
                            event_snapshot=event_snapshot,
                            relationship=relationships[0],
                            evaluation=evaluation,
                        )
                    except (StopIteration, TypeError, ValueError):
                        return None, ("EVENT_MARKET_LINEAGE_MISMATCH",)
                    refreshes[enriched.market_snapshot_id] = refresh
                    return enriched, ()

                enrichment = BuildEnrichedShadowMarketCohort.enrich(
                    campaign_id=campaign_id,
                    cycle_key=deterministic_id(
                        "multi_strategy_shared_cohort",
                        {"session_id": session_key, "ordinal": ordinal},
                    ),
                    configuration=configuration,
                    candidates=candidates,
                    enrich_candidate=enrich_candidate,
                    decision_at=datetime.now(tz=UTC),
                )
                stage = "ENRICHMENT_COMPLETED"
                cohort = enrichment.cohort
                if ordinal == 0:
                    selected_markets = tuple(item.market_ticker for item in cohort.selected)
                    print("multi-strategy selected_markets=" + ",".join(selected_markets))
                print(
                    "multi-strategy enrichment "
                    f"attempted={enrichment.attempted_count} "
                    f"succeeded={enrichment.successful_count} "
                    f"selected={len(cohort.selected)}"
                )
                stage = "COHORT_SELECTED"
                stage = "ITERATION_MARKETS_CAPTURED"
                orderbook_failures: list[str] = []
                for market_ordinal, member in enumerate(cohort.selected, start=1):
                    refresh = refresh_by_snapshot.get(member.market_snapshot_id)
                    if refresh is None:
                        orderbook_failures.append("ENRICHED_MARKET_REFRESH_MISSING")
                        continue
                    try:
                        evidence = capture.capture_shared_orderbook(
                            refresh=cast(Any, refresh),
                            maximum_depth=orderbook_depth,
                            timeout_seconds=10,
                            request_ordinal=10_000 + ordinal * 100 + market_ordinal,
                        )
                        multi_store.append_shared_orderbook(
                            raw=evidence.raw,
                            orderbook=evidence.orderbook,
                            levels=evidence.levels,
                        )
                    except PublicOrderbookCaptureTerminalError:
                        raise
                    except Exception as error:
                        orderbook_failures.append(
                            "ORDERBOOK_CAPTURE_FAILED_" + type(error).__name__
                        )
                stage = "ITERATION_ORDERBOOKS_CAPTURED"
                cycle = multi_strategy_shadow_cycle(
                    campaign_id=campaign_id,
                    idempotency_key=deterministic_id(
                        "multi_strategy_shadow_cycle",
                        {"session_id": session_key, "ordinal": ordinal},
                    ),
                    market_limit=market_limit,
                    maximum_predictions=100,
                    maximum_opportunities=40,
                    cohort_candidates=cohort.selected,
                    cohort_configuration=configuration,
                )
                stage = "STRATEGIES_INVOKED"
                stage = "COMPARISONS_COMPLETED"
                stage = "HYPOTHETICAL_ALLOCATION_COMPLETED"
                cycle_ids.append(str(cycle["cycle_id"]))
                completed += 1
                last_completed_stage = stage
                multi_store.record_multi_strategy_session_iteration(
                    session_id=session_key,
                    ordinal=ordinal,
                    cycle_id=str(cycle["cycle_id"]),
                    started_at=started,
                    completed_at=datetime.now(tz=UTC),
                    state="COMPLETED",
                    last_completed_stage=last_completed_stage,
                    reason_codes=tuple(
                        sorted(
                            {
                                "PUBLIC_GET_ONLY",
                                "NO_FINANCIAL_STATE_CREATED",
                                *(
                                    f"ENRICHMENT_{code}"
                                    for code, _count in enrichment.rejection_counts
                                ),
                                *orderbook_failures,
                            }
                        )
                    ),
                )
                print(f"multi-strategy iteration {ordinal + 1}/{iterations} completed")
            except Exception as error:
                failed += 1
                interruption_reason = type(error).__name__
                multi_store.record_multi_strategy_session_iteration(
                    session_id=session_key,
                    ordinal=ordinal,
                    cycle_id=None,
                    started_at=started,
                    completed_at=datetime.now(tz=UTC),
                    state="FAILED",
                    last_completed_stage=stage,
                    reason_codes=(
                        "PUBLIC_MULTI_STRATEGY_ITERATION_FAILED",
                        interruption_reason,
                    ),
                )
                print(
                    f"multi-strategy iteration {ordinal + 1}/{iterations} failed: "
                    f"{type(error).__name__}"
                )
                break
            if ordinal + 1 < iterations:
                time.sleep(interval_seconds)
        state = "COMPLETED" if completed == iterations else "PARTIAL"
        session_stage = "SESSION_COMPLETED" if state == "COMPLETED" else last_completed_stage
        multi_store.finalize_multi_strategy_session(
            session_id=session_key,
            state=state,
            completed_iterations=completed,
            failed_iterations=failed,
            last_completed_stage=session_stage,
            interruption_reason=interruption_reason,
            finalized_at=datetime.now(tz=UTC),
        )
        return {
            "session_id": session_key,
            "state": state,
            "iterations_requested": iterations,
            "completed_iterations": completed,
            "failed_iterations": failed,
            "selected_markets": selected_markets,
            "cycle_ids": tuple(cycle_ids),
            "financial_authority": "NONE",
        }
    finally:
        engine.dispose()


def reconcile_multi_strategy_shadow_session(*, session_id: str) -> dict[str, object]:
    """Reconcile one interrupted foreground session without public network access."""

    if not session_id or len(session_id) > 160:
        raise ValueError("multi-strategy session ID is invalid")
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_multi_strategy_shadow_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        reconciled = postgres_multi_strategy_shadow_store(engine).reconcile_multi_strategy_session(
            session_id=session_id,
            reconciled_at=datetime.now(tz=UTC),
            reason_code="EXPLICIT_FOREGROUND_SESSION_RECONCILIATION",
        )
        return {
            "session_id": session_id,
            "reconciled": reconciled,
            "network_access": "NONE",
            "financial_authority": "NONE",
        }
    finally:
        engine.dispose()


def create_shadow_plan(
    *,
    campaign_id: str,
    name: str,
    iterations: int,
    interval_seconds: int,
    market_count: int,
    maximum_pages: int,
    maximum_enrichments: int,
    orderbook_depth: int,
    idempotency_key: str,
    maximum_markets_per_event: int = 2,
    maximum_markets_per_series: int = 4,
    market_tickers: tuple[str, ...] = (),
) -> dict[str, object]:
    """Create one finite, local-only evidence plan without an execution path."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_shadow_operations_store,
    )
    from apex.application.shadow_operations import (
        CreateShadowEvidencePlanCommand,
        ShadowEvidencePlanConfiguration,
        fixed_strategy_configuration_bindings,
    )

    configuration = ShadowEvidencePlanConfiguration(
        iterations=iterations,
        interval_seconds=interval_seconds,
        market_count=market_count,
        max_pages=maximum_pages,
        max_enrichments=maximum_enrichments,
        max_markets_per_event=maximum_markets_per_event,
        max_markets_per_series=maximum_markets_per_series,
        orderbook_depth=orderbook_depth,
        strategy_configuration_bindings=fixed_strategy_configuration_bindings(
            market_count=market_count
        ),
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        plan, replay = postgres_shadow_operations_store(engine).create_plan(
            CreateShadowEvidencePlanCommand(
                campaign_id=campaign_id,
                name=name,
                configuration=configuration,
                idempotency_key=idempotency_key,
                requested_at=datetime.now(tz=UTC),
                requested_market_tickers=market_tickers,
            )
        )
        return {
            "plan_id": plan["plan_id"],
            "campaign_id": campaign_id,
            "state": plan["state"],
            "iterations": configuration.iterations,
            "market_count": configuration.market_count,
            "idempotent_replay": replay,
            "financial_authority": "NONE",
        }
    finally:
        engine.dispose()


def discover_create_shadow_plan(
    *,
    campaign_id: str,
    name: str,
    window_start: datetime,
    window_end: datetime,
    iterations: int,
    interval_seconds: int,
    target_markets: int,
    minimum_markets: int,
    minimum_events: int,
    maximum_pages: int,
    maximum_inspected_markets: int,
    maximum_enrichments: int,
    maximum_markets_per_event: int,
    maximum_markets_per_series: int,
    orderbook_depth: int,
    idempotency_key: str,
    maximum_orderbook_preflights: int = 80,
    microstructure_configuration_version: str = "orderbook-microstructure-shadow-v1",
    prior_plan_ids: tuple[str, ...] = (),
    exclude_prior_plan_markets: bool = True,
    exclude_prior_plan_events: bool = False,
    exclude_active_plan_markets: bool = True,
) -> dict[str, object]:
    """Run the sole one-pass public discovery/enrichment path for a new plan."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_multi_strategy_shadow_store,
        postgres_public_market_discovery_store,
        postgres_shadow_operations_store,
    )
    from apex.application.shadow_operations import (
        AtomicShadowLaunchLifecycleError,
        DiscoverAndCreateShadowEvidencePlan,
        DiscoverAndCreateShadowEvidencePlanCommand,
        ExactShadowCohortEnrichment,
        ExactShadowCohortEnrichmentAttempt,
    )
    from apex.domain.public_complement import PublicOrderbookLevel, PublicOrderbookSnapshot

    decision_at = datetime.now(tz=UTC)
    command = DiscoverAndCreateShadowEvidencePlanCommand(
        campaign_id=campaign_id,
        name=name,
        window_start=window_start,
        window_end=window_end,
        maximum_pages=maximum_pages,
        maximum_inspected_markets=maximum_inspected_markets,
        maximum_enrichments=maximum_enrichments,
        target_markets=target_markets,
        minimum_markets=minimum_markets,
        minimum_events=minimum_events,
        maximum_markets_per_event=maximum_markets_per_event,
        maximum_markets_per_series=maximum_markets_per_series,
        iterations=iterations,
        interval_seconds=interval_seconds,
        orderbook_depth=orderbook_depth,
        idempotency_key=idempotency_key,
        decision_at=decision_at,
        maximum_orderbook_preflights=maximum_orderbook_preflights,
        microstructure_configuration_version=microstructure_configuration_version,
        prior_plan_ids=prior_plan_ids,
        exclude_prior_plan_markets=exclude_prior_plan_markets,
        exclude_prior_plan_events=exclude_prior_plan_events,
        exclude_active_plan_markets=exclude_active_plan_markets,
    )
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        discovery_store = postgres_public_market_discovery_store(engine)
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        multi_store = postgres_multi_strategy_shadow_store(engine)
        plan_store = postgres_shadow_operations_store(engine)
        request_counts = {"exact": 0}
        market_client = KalshiPublicMarketDataClient()
        orderbook_client = KalshiPublicOrderbookDataClient()

        class _CountingMarketClient:
            def fetch(
                self,
                *,
                status: str,
                page_size: int,
                cursor: str | None,
                timeout_seconds: int,
                exclude_multivariate: bool,
            ) -> CapturedPublicMarketPage:
                return market_client.fetch(
                    status=status,
                    page_size=page_size,
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                    exclude_multivariate=exclude_multivariate,
                )

        class _CountingOrderbookClient:
            @staticmethod
            def _count() -> None:
                request_counts["exact"] += 1

            def fetch_orderbook(
                self, *, market_ticker: str, maximum_depth: int, timeout_seconds: int
            ) -> object:
                self._count()
                return orderbook_client.fetch_orderbook(
                    market_ticker=market_ticker,
                    maximum_depth=maximum_depth,
                    timeout_seconds=timeout_seconds,
                )

            def fetch_market(self, *, market_ticker: str, timeout_seconds: int) -> object:
                self._count()
                return orderbook_client.fetch_market(
                    market_ticker=market_ticker, timeout_seconds=timeout_seconds
                )

            def fetch_event(self, *, event_ticker: str, timeout_seconds: int) -> object:
                self._count()
                return orderbook_client.fetch_event(
                    event_ticker=event_ticker, timeout_seconds=timeout_seconds
                )

            def fetch_series(self, *, series_ticker: str, timeout_seconds: int) -> object:
                self._count()
                return orderbook_client.fetch_series(
                    series_ticker=series_ticker, timeout_seconds=timeout_seconds
                )

            def fetch_series_fee_changes(
                self, *, series_ticker: str, timeout_seconds: int
            ) -> object:
                self._count()
                return orderbook_client.fetch_series_fee_changes(
                    series_ticker=series_ticker, timeout_seconds=timeout_seconds
                )

        capture = CaptureBoundedPublicOrderbookSession(
            client=cast(Any, _CountingOrderbookClient()),
            store=capture_store,
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            normalizer=KalshiPublicMarketNormalizer(),
        )
        event_normalizer = KalshiPublicEventNormalizer()

        def discover(request: PublicMarketScanRequest) -> PublicMarketScanSummary:
            return DiscoverPublicPredictionMarkets(
                client=_CountingMarketClient(),
                normalizer=KalshiPublicMarketNormalizer(),
                store=discovery_store,
                clock=lambda: datetime.now(tz=UTC),
            ).discover(request)

        preflight_ordinal = 0

        def enrich(
            candidate: CaptureMarketCandidate, operation_id: str
        ) -> ExactShadowCohortEnrichmentAttempt:
            nonlocal preflight_ordinal
            request = BoundedOrderbookCaptureRequest(
                session_id=deterministic_id(
                    "atomic_shadow_cohort_enrichment",
                    {
                        "operation_id": operation_id,
                        "market_ticker": candidate.provider_market_ticker,
                    },
                ),
                market_ticker=candidate.provider_market_ticker,
                observations=3,
                interval_seconds=5,
                maximum_depth=command.orderbook_depth,
                maximum_bundle_quantity=Decimal("10"),
                timeout_seconds=command.timeout_seconds,
            )
            before = request_counts["exact"]
            attempt = capture.enrich_candidate(request=request, candidate=candidate)
            refresh = attempt.selected
            if refresh is None:
                return ExactShadowCohortEnrichmentAttempt(
                    selected=None,
                    reason_codes=attempt.reason_codes,
                    provider_get_count=request_counts["exact"] - before,
                    event_enrichment_succeeded=attempt.event_enrichment_succeeded,
                    series_enrichment_succeeded=attempt.series_enrichment_succeeded,
                    exact_market_refresh_succeeded=False,
                )
            try:
                event_raw = next(
                    raw
                    for raw in refresh.raw_responses
                    if isinstance(raw.response.document.get("event"), dict)
                )
                series_raw = next(
                    raw
                    for raw in refresh.raw_responses
                    if isinstance(raw.response.document.get("series"), dict)
                )
                event = event_raw.response.document["event"]
                if not isinstance(event, dict):
                    return ExactShadowCohortEnrichmentAttempt(
                        selected=None,
                        reason_codes=("EVENT_MARKET_LINEAGE_MISMATCH",),
                        provider_get_count=request_counts["exact"] - before,
                        event_enrichment_succeeded=True,
                        series_enrichment_succeeded=True,
                        exact_market_refresh_succeeded=True,
                    )
                event_snapshot = event_normalizer.normalize_events(
                    raw_response_id=event_raw.raw_response_id,
                    page=CapturedPublicEventPage(
                        endpoint_identity=event_raw.response.endpoint_identity,
                        request_cursor=None,
                        received_at=event_raw.response.received_at,
                        http_status=event_raw.response.http_status,
                        response_content_type=event_raw.response.response_content_type,
                        raw_payload=event_raw.response.raw_payload,
                        response_cursor=None,
                        document={"events": [event]},
                    ),
                )[0]
                relationships = link_event_markets(event_snapshot, (refresh.snapshot,))
                if (
                    len(relationships) != 1
                    or relationships[0].relationship_state.value != "EXACT"
                    or event_snapshot.provider_event_ticker != refresh.market.provider_event_ticker
                    or event_snapshot.series_ticker != refresh.market.series_ticker
                    or event_snapshot.data_quality_state.value != "ELIGIBLE"
                ):
                    return ExactShadowCohortEnrichmentAttempt(
                        selected=None,
                        reason_codes=("EVENT_MARKET_LINEAGE_MISMATCH",),
                        provider_get_count=request_counts["exact"] - before,
                        event_enrichment_succeeded=True,
                        series_enrichment_succeeded=True,
                        exact_market_refresh_succeeded=True,
                    )
                evaluation = evaluate_event_consistency(
                    event_snapshot,
                    relationships=relationships,
                    markets=(refresh.snapshot,),
                    evaluated_at=refresh.response.received_at,
                    evaluator_version="public-event-consistency-v1",
                )
                enriched = multi_store.persist_shared_enriched_market(
                    session_id=operation_id,
                    iteration_ordinal=0,
                    list_market_snapshot_id=candidate.market_snapshot_id,
                    refresh=refresh,
                    event_snapshot=event_snapshot,
                    relationship=relationships[0],
                    evaluation=evaluation,
                )
            except (IndexError, StopIteration, TypeError, ValueError):
                return ExactShadowCohortEnrichmentAttempt(
                    selected=None,
                    reason_codes=("EVENT_MARKET_LINEAGE_MISMATCH",),
                    provider_get_count=request_counts["exact"] - before,
                    event_enrichment_succeeded=True,
                    series_enrichment_succeeded=True,
                    exact_market_refresh_succeeded=True,
                )
            preflight_orderbook: PublicOrderbookSnapshot | None = None
            preflight_levels: tuple[PublicOrderbookLevel, ...] = ()
            preflight_attempted = False
            if command.requires_fee_compatible_preflight:
                try:
                    preflight_ordinal += 1
                    evidence = capture.capture_shared_orderbook(
                        refresh=refresh,
                        maximum_depth=command.orderbook_depth,
                        timeout_seconds=command.timeout_seconds,
                        request_ordinal=10_000 + preflight_ordinal,
                    )
                    multi_store.append_shared_orderbook(
                        raw=evidence.raw,
                        orderbook=evidence.orderbook,
                        levels=evidence.levels,
                    )
                except (PublicOrderbookCaptureError, ValueError):
                    return ExactShadowCohortEnrichmentAttempt(
                        selected=None,
                        reason_codes=("ORDERBOOK_PREFLIGHT_FAILED",),
                        provider_get_count=request_counts["exact"] - before,
                        event_enrichment_succeeded=True,
                        series_enrichment_succeeded=True,
                        exact_market_refresh_succeeded=True,
                        orderbook_preflight_attempted=True,
                    )
                preflight_orderbook = evidence.orderbook
                preflight_levels = evidence.levels
                preflight_attempted = True
            return ExactShadowCohortEnrichmentAttempt(
                selected=ExactShadowCohortEnrichment(
                    list_market_snapshot_id=candidate.market_snapshot_id,
                    market_snapshot_id=enriched.market_snapshot_id,
                    event_snapshot_id=event_snapshot.snapshot_id,
                    event_raw_response_id=event_raw.raw_response_id,
                    series_raw_response_id=series_raw.raw_response_id,
                    exact_market_raw_response_id=refresh.raw_response_id,
                    fee_snapshot_id=(
                        refresh.initial_fee.fee_snapshot_id
                        if refresh.initial_fee is not None
                        else None
                    ),
                    provider=enriched.provider,
                    market_ticker=enriched.market_ticker,
                    event_ticker=enriched.event_ticker,
                    series_ticker=enriched.series_ticker,
                    rule_content_hash=enriched.rule_content_hash.value,
                    settlement_source_identity=event_snapshot.settlement_source_hash.value,
                    lifecycle_state=enriched.lifecycle_state,
                    binary_structure_identity=content_digest(
                        {
                            "market_snapshot_id": enriched.market_snapshot_id,
                            "structure": "BINARY_YES_NO",
                        }
                    ),
                    observed_at=enriched.observed_at,
                    available_at=enriched.available_at,
                    close_at=enriched.close_at,
                    market_lineage_hash=enriched.market_lineage_hash.value,
                    event_lineage_hash=enriched.event_lineage_hash.value,
                    series_lineage_hash=enriched.series_lineage_hash.value,
                    fee_lineage_hash=(
                        enriched.fee_lineage_hash.value
                        if enriched.fee_lineage_hash is not None
                        else None
                    ),
                    enriched_at=refresh.response.received_at,
                    preflight_orderbook=preflight_orderbook,
                    preflight_levels=preflight_levels,
                    preflight_fee=refresh.initial_fee,
                ),
                reason_codes=(),
                provider_get_count=request_counts["exact"] - before,
                event_enrichment_succeeded=True,
                series_enrichment_succeeded=True,
                exact_market_refresh_succeeded=True,
                orderbook_preflight_attempted=preflight_attempted,
            )

        try:
            result = DiscoverAndCreateShadowEvidencePlan(
                store=plan_store,
                discover=discover,
                select_candidates=lambda run_id, limit: capture_store.select_capture_candidates(
                    ingestion_run_id=run_id, limit=limit
                ),
                enrich_candidate=enrich,
            ).run(command)
        except AtomicShadowLaunchLifecycleError as error:
            return {
                "state": error.state.value,
                "replay": False,
                "provider_get_count": 0,
                "provider_order_count": 0,
                "financial_authority": "NONE",
            }
        return {
            **result.summary.as_dict(),
            "retained_provider_get_count": result.summary.provider_get_count,
            "provider_get_count": 0
            if result.summary.idempotent_replay
            else result.summary.provider_get_count,
            "state": result.state.value,
            "replay": result.summary.idempotent_replay,
            "result_hash": result.result_hash,
            "financial_authority": "NONE",
        }
    finally:
        engine.dispose()


def _parse_market_tickers(value: str | None) -> tuple[str, ...]:
    """Parse the bounded CLI form without accepting empty implicit selections."""

    if value is None:
        return ()
    values = tuple(value.split(","))
    if not values or any(not item for item in values):
        raise ValueError("market tickers must be a comma-separated list without empty values")
    return values


def _frozen_plan_members(rows: tuple[dict[str, object], ...]) -> tuple[object, ...]:
    from apex.domain.multi_strategy import EnrichedShadowMarket

    return tuple(
        EnrichedShadowMarket(
            market_snapshot_id=str(row["market_snapshot_id"]),
            provider=str(row["provider"]),
            market_ticker=str(row["market_ticker"]),
            event_ticker=str(row["event_ticker"]),
            series_ticker=str(row["series_ticker"]),
            rule_content_hash=ContentHash(str(row["rule_content_hash"])),
            lifecycle_state=str(row["lifecycle_state"]),
            observed_at=cast(datetime, row["observed_at"]),
            available_at=cast(datetime, row["available_at"]),
            close_at=cast(datetime, row["close_at"]),
            market_lineage_hash=ContentHash(str(row["market_lineage_hash"])),
            event_lineage_hash=ContentHash(str(row["event_lineage_hash"])),
            series_lineage_hash=ContentHash(str(row["series_lineage_hash"])),
            fee_lineage_hash=(
                ContentHash(str(row["fee_lineage_hash"]))
                if row["fee_lineage_hash"] is not None
                else None
            ),
        )
        for row in rows
    )


def run_shadow_plan_iteration(*, plan_id: str, owner_id: str) -> dict[str, object]:
    """Run at most one due plan iteration with a fenced local worker lease."""

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_bounded_orderbook_capture_store,
        postgres_multi_strategy_shadow_store,
        postgres_public_market_discovery_store,
        postgres_shadow_operations_store,
    )
    from apex.application.capture_public_orderbook_session import (
        PublicOrderbookCaptureTerminalError,
    )
    from apex.application.multi_strategy_shadow import BuildEnrichedShadowMarketCohort
    from apex.application.shadow_operations import (
        FrozenShadowEvidenceCohortMember,
        ShadowEvidenceIterationState,
        ShadowEvidencePlanConfiguration,
        ShadowPlanConfigurationBindingError,
        resolve_plan_strategy_configurations,
    )
    from apex.domain.market_dynamics import MarketDynamicsStrategyConfig
    from apex.domain.multi_strategy import EnrichedShadowMarket, ShadowMarketCohortConfiguration
    from apex.domain.orderbook_microstructure import OrderbookMicrostructureStrategyConfig

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        plan_store = postgres_shadow_operations_store(engine)
        now = datetime.now(tz=UTC)
        leased, fence = plan_store.acquire_lease(plan_id=plan_id, owner_id=owner_id, now=now)
        if not leased:
            return {
                "plan_id": plan_id,
                "state": "LEASED_BY_ANOTHER_WORKER",
                "financial_authority": "NONE",
            }
        iteration = plan_store.begin_due_iteration(plan_id=plan_id, owner_id=owner_id, now=now)
        if iteration is None:
            plan = plan_store.plan(plan_id)
            plan_store.release_lease(
                plan_id=plan_id,
                owner_id=owner_id,
                fence=cast(int, fence),
                released_at=datetime.now(tz=UTC),
            )
            return {
                "plan_id": plan_id,
                "state": plan["state"] if plan is not None else "MISSING",
                "due_iteration": False,
                "financial_authority": "NONE",
            }
        plan_configuration = cast(ShadowEvidencePlanConfiguration, iteration["configuration"])
        discovery_store = postgres_public_market_discovery_store(engine)
        capture_store = postgres_bounded_orderbook_capture_store(engine)
        multi_store = postgres_multi_strategy_shadow_store(engine)
        capture = CaptureBoundedPublicOrderbookSession(
            client=KalshiPublicOrderbookDataClient(),
            store=capture_store,
            wall_clock=lambda: datetime.now(tz=UTC),
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            normalizer=KalshiPublicMarketNormalizer(),
        )
        event_normalizer = KalshiPublicEventNormalizer()
        ordinal = cast(int, iteration["ordinal"])
        stage = "LEASE_ACQUIRED"
        reason_codes: list[str] = ["PUBLIC_GET_ONLY", "NO_FINANCIAL_STATE_CREATED"]
        cycle: dict[str, object] | None = None
        refresh_count = 0
        book_successes = 0
        book_failures = 0
        try:
            # Reproduce the plan's exact executable configurations before this
            # worker can make a provider request or create a prediction.
            plan_strategy_configurations = resolve_plan_strategy_configurations(plan_configuration)
            reason_codes.append("PLAN_EXACT_STRATEGY_CONFIGURATION_BOUND")
            micro_configuration = cast(
                OrderbookMicrostructureStrategyConfig,
                plan_strategy_configurations["kalshi-orderbook-microstructure-v1"],
            )
            dynamics_configuration = cast(
                MarketDynamicsStrategyConfig,
                plan_strategy_configurations["kalshi-market-dynamics-v1"],
            )
            required_observations = max(
                int(micro_configuration.minimum_observations_per_market),
                int(dynamics_configuration.minimum_observations),
            )
            cohort_rows = plan_store.cohort(plan_id)
            plan = plan_store.plan(plan_id)
            requested_market_tickers = (
                cast(tuple[str, ...], plan["requested_market_tickers"]) if plan is not None else ()
            )
            shared_configuration = ShadowMarketCohortConfiguration(
                maximum_pages=plan_configuration.max_pages,
                maximum_inspected_markets=max(
                    20, plan_configuration.market_count * plan_configuration.max_pages
                ),
                maximum_enrichments=max(3, min(80, plan_configuration.max_enrichments)),
                selected_market_count=max(3, plan_configuration.market_count),
                maximum_markets_per_event=plan_configuration.max_markets_per_event,
                maximum_markets_per_series=plan_configuration.max_markets_per_series,
                required_observations=required_observations,
            )
            if not cohort_rows:
                if requested_market_tickers:
                    candidates = capture_store.select_capture_candidates_by_tickers(
                        market_tickers=requested_market_tickers
                    )
                    retained_tickers = {
                        candidate.provider_market_ticker for candidate in candidates
                    }
                    missing_tickers = tuple(
                        ticker
                        for ticker in requested_market_tickers
                        if ticker not in retained_tickers
                    )
                    if missing_tickers:
                        raise PublicOrderbookCaptureTerminalError(
                            "explicit shadow plan market tickers are not retained eligible "
                            "candidates: " + ",".join(missing_tickers)
                        )
                    stage = "EXPLICIT_COHORT_CANDIDATES_RETAINED"
                else:
                    summary = DiscoverPublicPredictionMarkets(
                        client=KalshiPublicMarketDataClient(),
                        normalizer=KalshiPublicMarketNormalizer(),
                        store=discovery_store,
                        clock=lambda: datetime.now(tz=UTC),
                    ).discover(
                        PublicMarketScanRequest(
                            run_key=deterministic_id(
                                "shadow_evidence_plan_market_discovery",
                                {"plan_id": plan_id, "ordinal": ordinal},
                            ),
                            page_size=min(100, max(20, plan_configuration.market_count)),
                            maximum_pages=plan_configuration.max_pages,
                            timeout_seconds=plan_configuration.timeout_seconds,
                        )
                    )
                    stage = "MARKET_DISCOVERY_COMPLETED"
                    candidates = capture_store.select_capture_candidates(
                        ingestion_run_id=summary.ingestion_run_id,
                        limit=min(80, plan_configuration.max_enrichments),
                    )
                refreshes: dict[str, Any] = {}

                def enrich_candidate(
                    candidate: object,
                ) -> tuple[EnrichedShadowMarket | None, tuple[str, ...]]:
                    if not isinstance(candidate, CaptureMarketCandidate):
                        return None, ("UNSUPPORTED_MARKET_STRUCTURE",)
                    request = BoundedOrderbookCaptureRequest(
                        session_id=deterministic_id(
                            "shadow_evidence_plan_enrichment",
                            {
                                "plan_id": plan_id,
                                "ordinal": ordinal,
                                "ticker": candidate.provider_market_ticker,
                            },
                        ),
                        market_ticker=candidate.provider_market_ticker,
                        observations=required_observations,
                        interval_seconds=5,
                        maximum_depth=plan_configuration.orderbook_depth,
                        maximum_bundle_quantity=Decimal("10"),
                        timeout_seconds=plan_configuration.timeout_seconds,
                    )
                    attempt = capture.enrich_candidate(request=request, candidate=candidate)
                    refresh = attempt.selected
                    if refresh is None:
                        return None, attempt.reason_codes
                    try:
                        event_raw = next(
                            raw
                            for raw in refresh.raw_responses
                            if isinstance(raw.response.document.get("event"), dict)
                        )
                        event = event_raw.response.document["event"]
                        assert isinstance(event, dict)
                        event_snapshot = event_normalizer.normalize_events(
                            raw_response_id=event_raw.raw_response_id,
                            page=CapturedPublicEventPage(
                                endpoint_identity=event_raw.response.endpoint_identity,
                                request_cursor=None,
                                received_at=event_raw.response.received_at,
                                http_status=event_raw.response.http_status,
                                response_content_type=event_raw.response.response_content_type,
                                raw_payload=event_raw.response.raw_payload,
                                response_cursor=None,
                                document={"events": [event]},
                            ),
                        )[0]
                        relationships = link_event_markets(event_snapshot, (refresh.snapshot,))
                        if len(relationships) != 1:
                            return None, ("EVENT_MARKET_LINEAGE_MISMATCH",)
                        evaluation = evaluate_event_consistency(
                            event_snapshot,
                            relationships=relationships,
                            markets=(refresh.snapshot,),
                            evaluated_at=refresh.response.received_at,
                            evaluator_version="public-event-consistency-v1",
                        )
                        enriched = multi_store.persist_shared_enriched_market(
                            session_id=plan_id,
                            iteration_ordinal=ordinal,
                            list_market_snapshot_id=candidate.market_snapshot_id,
                            refresh=refresh,
                            event_snapshot=event_snapshot,
                            relationship=relationships[0],
                            evaluation=evaluation,
                        )
                    except (IndexError, StopIteration, TypeError, ValueError):
                        return None, ("EVENT_MARKET_LINEAGE_MISMATCH",)
                    refreshes[enriched.market_snapshot_id] = refresh
                    return enriched, ()

                enrichment = BuildEnrichedShadowMarketCohort.enrich(
                    campaign_id=str(iteration["campaign_id"]),
                    cycle_key=deterministic_id("shadow_evidence_plan_cohort", {"plan_id": plan_id}),
                    configuration=shared_configuration,
                    candidates=candidates,
                    enrich_candidate=enrich_candidate,
                    decision_at=datetime.now(tz=UTC),
                )
                stage = "COHORT_ENRICHED"
                if requested_market_tickers:
                    selected_tickers = tuple(
                        member.market_ticker for member in enrichment.cohort.selected
                    )
                    if set(selected_tickers) != set(requested_market_tickers) or len(
                        selected_tickers
                    ) != len(requested_market_tickers):
                        raise PublicOrderbookCaptureTerminalError(
                            "explicit shadow plan market tickers did not all pass exact "
                            "market/event/series/rule validation"
                        )
                plan_store.freeze_cohort(
                    plan_id=plan_id,
                    members=tuple(
                        FrozenShadowEvidenceCohortMember(
                            market_snapshot_id=member.market_snapshot_id,
                            provider=member.provider,
                            market_ticker=member.market_ticker,
                            event_ticker=member.event_ticker,
                            series_ticker=member.series_ticker,
                            rule_content_hash=member.rule_content_hash.value,
                            lifecycle_state=member.lifecycle_state,
                            observed_at=member.observed_at,
                            available_at=member.available_at,
                            close_at=member.close_at,
                            market_lineage_hash=member.market_lineage_hash.value,
                            event_lineage_hash=member.event_lineage_hash.value,
                            series_lineage_hash=member.series_lineage_hash.value,
                            fee_lineage_hash=member.fee_lineage_hash.value
                            if member.fee_lineage_hash
                            else None,
                        )
                        for member in enrichment.cohort.selected
                    ),
                    frozen_at=datetime.now(tz=UTC),
                )
                cohort = enrichment.cohort.selected
                reason_codes.extend(
                    f"ENRICHMENT_{code}" for code, _count in enrichment.rejection_counts
                )
            else:
                # Reuse only the original frozen identities.  Each iteration still
                # records fresh exact market/event/series/fee evidence for those
                # identities; it never calls discovery or substitutes a market.
                refreshed_members: list[EnrichedShadowMarket] = []
                refreshes = {}
                for member in cohort_rows:
                    candidate = CaptureMarketCandidate(
                        market_snapshot_id=str(member["market_snapshot_id"]),
                        ingestion_run_id=deterministic_id(
                            "shadow_evidence_frozen_member",
                            {"plan_id": plan_id, "market_ticker": member["market_ticker"]},
                        ),
                        provider_market_ticker=str(member["market_ticker"]),
                        provider_event_ticker=str(member["event_ticker"]),
                        status="open",
                        observed_at=cast(datetime, member["observed_at"]),
                        close_time=cast(datetime, member["close_at"]),
                        is_provisional=False,
                        rule_content_hash=ContentHash(str(member["rule_content_hash"])),
                        data_quality_state="ELIGIBLE",
                    )
                    request = BoundedOrderbookCaptureRequest(
                        session_id=deterministic_id(
                            "shadow_evidence_frozen_enrichment",
                            {
                                "plan_id": plan_id,
                                "ordinal": ordinal,
                                "ticker": candidate.provider_market_ticker,
                            },
                        ),
                        market_ticker=candidate.provider_market_ticker,
                        observations=required_observations,
                        interval_seconds=5,
                        maximum_depth=plan_configuration.orderbook_depth,
                        maximum_bundle_quantity=Decimal("10"),
                        timeout_seconds=plan_configuration.timeout_seconds,
                    )
                    attempt = capture.enrich_candidate(request=request, candidate=candidate)
                    refresh = attempt.selected
                    if refresh is None:
                        reason_codes.extend(
                            f"FROZEN_MEMBER_{code}" for code in attempt.reason_codes
                        )
                        continue
                    try:
                        event_raw = next(
                            raw
                            for raw in refresh.raw_responses
                            if isinstance(raw.response.document.get("event"), dict)
                        )
                        event = event_raw.response.document["event"]
                        assert isinstance(event, dict)
                        event_snapshot = event_normalizer.normalize_events(
                            raw_response_id=event_raw.raw_response_id,
                            page=CapturedPublicEventPage(
                                endpoint_identity=event_raw.response.endpoint_identity,
                                request_cursor=None,
                                received_at=event_raw.response.received_at,
                                http_status=event_raw.response.http_status,
                                response_content_type=event_raw.response.response_content_type,
                                raw_payload=event_raw.response.raw_payload,
                                response_cursor=None,
                                document={"events": [event]},
                            ),
                        )[0]
                        relationships = link_event_markets(event_snapshot, (refresh.snapshot,))
                        if (
                            refresh.snapshot.provider_market_ticker != member["market_ticker"]
                            or event_snapshot.provider_event_ticker != member["event_ticker"]
                            or event_snapshot.series_ticker != member["series_ticker"]
                            or refresh.snapshot.rule_content_hash is None
                            or refresh.snapshot.rule_content_hash.value
                            != member["rule_content_hash"]
                            or len(relationships) != 1
                        ):
                            reason_codes.append("FROZEN_MEMBER_IDENTITY_CHANGED")
                            continue
                        evaluation = evaluate_event_consistency(
                            event_snapshot,
                            relationships=relationships,
                            markets=(refresh.snapshot,),
                            evaluated_at=refresh.response.received_at,
                            evaluator_version="public-event-consistency-v1",
                        )
                        enriched = multi_store.persist_shared_enriched_market(
                            session_id=plan_id,
                            iteration_ordinal=ordinal,
                            list_market_snapshot_id=str(member["market_snapshot_id"]),
                            refresh=refresh,
                            event_snapshot=event_snapshot,
                            relationship=relationships[0],
                            evaluation=evaluation,
                        )
                    except (IndexError, StopIteration, TypeError, ValueError):
                        reason_codes.append("FROZEN_MEMBER_EVENT_MARKET_LINEAGE_MISMATCH")
                        continue
                    refreshes[enriched.market_snapshot_id] = refresh
                    refreshed_members.append(enriched)
                cohort = tuple(refreshed_members)
                stage = "FROZEN_COHORT_REFRESHED"
            if not cohort:
                completed = plan_store.complete_iteration(
                    iteration_id=str(iteration["iteration_id"]),
                    lease_fence=cast(int, fence),
                    state=ShadowEvidenceIterationState.PARTIAL,
                    completed_at=datetime.now(tz=UTC),
                    cycle_id=None,
                    cohort_size=0,
                    market_refresh_count=0,
                    orderbook_success_count=0,
                    orderbook_failure_count=0,
                    prediction_count=0,
                    comparison_count=0,
                    conflict_count=0,
                    hypothetical_admission_count=0,
                    hypothetical_rejection_count=0,
                    provider_request_count=0,
                    last_completed_stage=stage,
                    reason_codes=tuple([*reason_codes, "FROZEN_COHORT_EMPTY"]),
                )
                plan_store.release_lease(
                    plan_id=plan_id,
                    owner_id=owner_id,
                    fence=cast(int, fence),
                    released_at=datetime.now(tz=UTC),
                )
                return {"plan_id": plan_id, "iteration": completed, "financial_authority": "NONE"}
            for market_ordinal, cohort_member in enumerate(cohort, start=1):
                refresh = refreshes.get(cohort_member.market_snapshot_id)
                if refresh is None:
                    # A frozen cohort is never silently replaced.  An exact refresh on a later
                    # iteration is represented as unavailable rather than selecting another market.
                    reason_codes.append("FROZEN_MARKET_REFRESH_UNAVAILABLE")
                    continue
                refresh_count += 1
                try:
                    evidence = capture.capture_shared_orderbook(
                        refresh=refresh,
                        maximum_depth=plan_configuration.orderbook_depth,
                        timeout_seconds=plan_configuration.timeout_seconds,
                        request_ordinal=50_000 + ordinal * 100 + market_ordinal,
                    )
                    multi_store.append_shared_orderbook(
                        raw=evidence.raw, orderbook=evidence.orderbook, levels=evidence.levels
                    )
                    book_successes += 1
                except PublicOrderbookCaptureTerminalError:
                    raise
                except Exception as error:
                    book_failures += 1
                    reason_codes.append("ORDERBOOK_CAPTURE_FAILED_" + type(error).__name__)
            stage = "ITERATION_ORDERBOOKS_CAPTURED"
            cycle = multi_strategy_shadow_cycle(
                campaign_id=str(iteration["campaign_id"]),
                idempotency_key=deterministic_id(
                    "shadow_evidence_plan_cycle", {"plan_id": plan_id, "ordinal": ordinal}
                ),
                market_limit=len(cohort),
                maximum_predictions=plan_configuration.maximum_predictions_per_iteration,
                maximum_opportunities=plan_configuration.maximum_opportunities_per_iteration,
                cohort_candidates=cast(tuple[object, ...], cohort),
                cohort_configuration=shared_configuration,
                strategy_configurations=plan_strategy_configurations,
            )
            stage = "HYPOTHETICAL_ALLOCATION_COMPLETED"
            completed = plan_store.complete_iteration(
                iteration_id=str(iteration["iteration_id"]),
                lease_fence=cast(int, fence),
                state=(
                    ShadowEvidenceIterationState.COMPLETED
                    if book_failures == 0
                    else ShadowEvidenceIterationState.PARTIAL
                ),
                completed_at=datetime.now(tz=UTC),
                cycle_id=str(cycle["cycle_id"]),
                cohort_size=len(cohort),
                market_refresh_count=refresh_count,
                orderbook_success_count=book_successes,
                orderbook_failure_count=book_failures,
                prediction_count=cast(int, cycle["prediction_count"]),
                comparison_count=cast(int, cycle["comparison_count"]),
                conflict_count=cast(int, cycle["conflict_count"]),
                hypothetical_admission_count=cast(int, cycle["hypothetical_admission_count"]),
                hypothetical_rejection_count=cast(int, cycle["hypothetical_rejection_count"]),
                provider_request_count=refresh_count + book_successes + book_failures,
                last_completed_stage=stage,
                reason_codes=tuple(reason_codes),
            )
            plan_store.release_lease(
                plan_id=plan_id,
                owner_id=owner_id,
                fence=cast(int, fence),
                released_at=datetime.now(tz=UTC),
            )
            return {
                "plan_id": plan_id,
                "iteration": completed,
                "cycle": cycle,
                "financial_authority": "NONE",
            }
        except Exception as error:
            completed = plan_store.complete_iteration(
                iteration_id=str(iteration["iteration_id"]),
                lease_fence=cast(int, fence),
                state=ShadowEvidenceIterationState.FAILED,
                completed_at=datetime.now(tz=UTC),
                cycle_id=str(cycle["cycle_id"]) if cycle is not None else None,
                cohort_size=len(plan_store.cohort(plan_id)),
                market_refresh_count=refresh_count,
                orderbook_success_count=book_successes,
                orderbook_failure_count=book_failures,
                prediction_count=0,
                comparison_count=0,
                conflict_count=0,
                hypothetical_admission_count=0,
                hypothetical_rejection_count=0,
                provider_request_count=refresh_count + book_successes + book_failures,
                last_completed_stage=stage,
                reason_codes=tuple(
                    [
                        *reason_codes,
                        "SHADOW_PLAN_ITERATION_FAILED",
                        (
                            str(error)
                            if isinstance(error, ShadowPlanConfigurationBindingError)
                            else type(error).__name__
                        ),
                    ]
                ),
            )
            plan_store.release_lease(
                plan_id=plan_id,
                owner_id=owner_id,
                fence=cast(int, fence),
                released_at=datetime.now(tz=UTC),
            )
            return {
                "plan_id": plan_id,
                "iteration": completed,
                "error": type(error).__name__,
                "financial_authority": "NONE",
            }
    finally:
        engine.dispose()


def shadow_worker(
    *, plan_id: str | None, once: bool, poll_seconds: int, maximum_plans: int
) -> dict[str, object]:
    """Run a manually invoked, bounded local worker; it has no scheduler or API trigger."""

    if not 5 <= poll_seconds <= 300:
        raise ValueError("shadow worker poll seconds must be between five and 300")
    if not 1 <= maximum_plans <= 100:
        raise ValueError("shadow worker maximum plans must be between one and 100")
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_shadow_operations_store,
    )

    owner_id = deterministic_id(
        "shadow_worker", {"started_at": datetime.now(tz=UTC), "plan_id": plan_id}
    )
    processed: list[dict[str, object]] = []
    plan_ids: tuple[str, ...]
    while True:
        engine = create_postgres_engine(PostgresSettings.from_env())
        try:
            store = postgres_shadow_operations_store(engine)
            if plan_id is not None:
                plan_ids = (plan_id,)
            else:
                plan_ids = store.due_plan_ids(now=datetime.now(tz=UTC), limit=maximum_plans)
        finally:
            engine.dispose()
        for current_plan_id in plan_ids:
            try:
                processed.append(
                    run_shadow_plan_iteration(plan_id=current_plan_id, owner_id=owner_id)
                )
            except Exception as error:
                engine = create_postgres_engine(PostgresSettings.from_env())
                try:
                    recovery_store = postgres_shadow_operations_store(engine)
                    lease = recovery_store.lease(current_plan_id)
                    interrupted = (
                        recovery_store.interrupt_owned_iteration(
                            plan_id=current_plan_id,
                            owner_id=owner_id,
                            fence=cast(int, lease["fence"]),
                            interrupted_at=datetime.now(tz=UTC),
                            reason_code="LOCAL_WORKER_EXCEPTION_" + type(error).__name__,
                        )
                        if lease is not None and lease["owner_id"] == owner_id
                        else False
                    )
                finally:
                    engine.dispose()
                processed.append(
                    {
                        "plan_id": current_plan_id,
                        "state": "INTERRUPTED" if interrupted else "FAILED",
                        "error": type(error).__name__,
                        "financial_authority": "NONE",
                    }
                )
        if once:
            break
        if plan_id is None:
            if not plan_ids:
                break
            time.sleep(poll_seconds)
            continue
        engine = create_postgres_engine(PostgresSettings.from_env())
        try:
            current = postgres_shadow_operations_store(engine).plan(plan_id)
            if current is None or current["state"] in (
                "COMPLETED",
                "PARTIAL",
                "FAILED",
                "CANCELED",
                "INTERRUPTED",
            ):
                break
        finally:
            engine.dispose()
        time.sleep(poll_seconds)
    return {"processed": tuple(processed), "financial_authority": "NONE", "automatic_start": False}


def collect_shadow_outcomes(*, campaign_id: str, maximum_markets: int) -> dict[str, object]:
    """Collect exact public outcomes with strict final-identity reconciliation only."""

    if not 1 <= maximum_markets <= 500:
        raise ValueError("maximum outcome markets must be between one and 500")
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_shadow_operations_store,
    )
    from apex.application.shadow_operations import PublicOutcomeState

    engine = create_postgres_engine(PostgresSettings.from_env())
    facts: list[str] = []
    exact_market_gets = 0
    exact_event_gets = 0
    final_markets = 0
    still_active_markets = 0
    compatible_outcomes = 0
    genuine_incompatible_outcomes = 0
    fact_replays = 0
    new_facts = 0
    scoring_failures = 0
    try:
        store = postgres_shadow_operations_store(engine)
        plans = store.plans(campaign_id=campaign_id, limit=100)
        members_by_ticker: dict[str, tuple[str, RetainedPublicOutcomeIdentity]] = {}
        for plan in plans:
            plan_id = str(plan["plan_id"])
            for member in store.cohort(plan_id):
                market_ticker = str(member["market_ticker"])
                identity = store.retained_public_outcome_identity(
                    plan_id=plan_id, market_ticker=market_ticker
                )
                members_by_ticker[market_ticker] = (
                    plan_id,
                    RetainedPublicOutcomeIdentity(
                        market_ticker=str(identity["market_ticker"]),
                        event_ticker=str(identity["event_ticker"]),
                        series_ticker=str(identity["series_ticker"]),
                        rule_content_hash=str(identity["rule_content_hash"]),
                        settlement_source_hash=str(identity["settlement_source_hash"]),
                    ),
                )
        tickers = tuple(members_by_ticker)[:maximum_markets]
        client = KalshiPublicOrderbookDataClient()
        for ticker in tickers:
            response = None
            for attempt in range(2):
                try:
                    response = client.fetch_market(market_ticker=ticker, timeout_seconds=10)
                    exact_market_gets += 1
                    break
                except Exception:
                    exact_market_gets += 1
                    if attempt:
                        raise
            assert response is not None
            market = response.document.get("market")
            plan_id_for_ticker, retained = members_by_ticker[ticker]
            reconciliation = reconcile_public_final_outcome(
                retained=retained,
                final_market=market,
                exact_event=None,
            )
            reconciliation_raw_response_ids: tuple[str, ...] = ()
            if reconciliation.requires_exact_event_lookup:
                exact_event_gets += 1
                try:
                    event_response = client.fetch_event(
                        event_ticker=retained.event_ticker, timeout_seconds=10
                    )
                except Exception as error:
                    reconciliation = reconcile_public_final_outcome(
                        retained=retained,
                        final_market=market,
                        exact_event={},
                    )
                    reconciliation = reconciliation.__class__(
                        state=PublicOutcomeReconciliationState.INVALID,
                        resolved_outcome=None,
                        rule_content_hash=reconciliation.rule_content_hash,
                        event_ticker=reconciliation.event_ticker,
                        series_ticker=reconciliation.series_ticker,
                        reason_codes=tuple(
                            sorted(
                                set(
                                    (
                                        *reconciliation.reason_codes,
                                        "EXACT_EVENT_LOOKUP_FAILED_" + type(error).__name__,
                                    )
                                )
                            )
                        ),
                    )
                else:
                    event_raw_response_id = (
                        store.record_public_outcome_reconciliation_event_response(
                            campaign_id=campaign_id,
                            plan_id=plan_id_for_ticker,
                            market_ticker=ticker,
                            event_ticker=retained.event_ticker,
                            response=event_response,
                        )
                    )
                    reconciliation_raw_response_ids = (event_raw_response_id,)
                    reconciliation = reconcile_public_final_outcome(
                        retained=retained,
                        final_market=market,
                        exact_event=event_response.document.get("event"),
                    )
            if reconciliation.state is PublicOutcomeReconciliationState.UNRESOLVED:
                still_active_markets += 1
            elif reconciliation.state is PublicOutcomeReconciliationState.RESOLVED:
                final_markets += 1
                compatible_outcomes += 1
            else:
                final_markets += 1
                genuine_incompatible_outcomes += 1
            fact, _replay = store.record_public_outcome_fact(
                campaign_id=campaign_id,
                plan_id=plan_id_for_ticker,
                market_ticker=ticker,
                raw_payload=response.raw_payload,
                received_at=response.received_at,
                endpoint_identity=response.endpoint_identity,
                response_content_type=response.response_content_type,
                resolved_outcome=reconciliation.resolved_outcome,
                state=PublicOutcomeState(reconciliation.state.value),
                rule_content_hash=reconciliation.rule_content_hash,
                reason_codes=reconciliation.reason_codes,
                event_ticker=reconciliation.event_ticker,
                series_ticker=reconciliation.series_ticker,
                provider_result_classification=(
                    str(market.get("status")) if isinstance(market, dict) else None
                ),
                reconciliation_raw_response_ids=reconciliation_raw_response_ids,
            )
            if _replay:
                fact_replays += 1
            else:
                new_facts += 1
            if reconciliation.state is PublicOutcomeReconciliationState.RESOLVED:
                facts.append(str(fact["outcome_fact_id"]))
        scored = 0
        for fact_id in facts:
            created, skipped = store.evaluate_public_outcome_fact(
                outcome_fact_id=fact_id, evaluated_at=datetime.now(tz=UTC)
            )
            scored += created
            scoring_failures += skipped
        return {
            "outcome_fact_ids": tuple(facts),
            "scored_predictions": scored,
            "markets_considered": len(tickers),
            "exact_market_gets": exact_market_gets,
            "exact_event_gets": exact_event_gets,
            "exact_series_gets": 0,
            "final_markets": final_markets,
            "still_active_markets": still_active_markets,
            "compatible_outcomes": compatible_outcomes,
            "genuine_incompatible_outcomes": genuine_incompatible_outcomes,
            "existing_fact_replays": fact_replays,
            "new_public_outcome_facts": new_facts,
            "scoring_failures": scoring_failures,
            "provider_order_calls": 0,
            "financial_authority": "NONE",
        }
    finally:
        engine.dispose()


def strategy_performance(
    *, campaign_id: str, strategy_id: str | None, plan_id: str | None
) -> dict[str, object]:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_shadow_operations_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        return postgres_shadow_operations_store(engine).performance_report(
            campaign_id=campaign_id,
            strategy_id=strategy_id,
            plan_id=plan_id,
            requested_at=datetime.now(tz=UTC),
        )
    finally:
        engine.dispose()


def shadow_operations_demo() -> dict[str, object]:
    """Network-free deterministic scoring proof; it creates no campaign or provider call."""

    from apex.application.shadow_operations import score_shadow_prediction_against_public_outcome

    score = score_shadow_prediction_against_public_outcome(
        point_probabilities=(("NO", Decimal("0.30")), ("YES", Decimal("0.70"))),
        conservative_probabilities=(("NO", Decimal("0.40")), ("YES", Decimal("0.60"))),
        consensus_probabilities=(("NO", Decimal("0.50")), ("YES", Decimal("0.50"))),
        resolved_outcome="YES",
    )
    return {
        "finite_plan": True,
        "frozen_cohort_replacement": False,
        "point_brier_score": score.point_brier_score,
        "conservative_brier_score": score.conservative_brier_score,
        "consensus_brier_score": score.consensus_brier_score,
        "deterministic_decimal_log_loss": score.point_log_loss,
        "provider_calls": 0,
        "financial_authority": "NONE",
        "orders_created": 0,
        "positions_changed": 0,
        "ledger_journals_created": 0,
    }


def multi_strategy_shadow_demo() -> dict[str, object]:
    """Network-free proof of shared lineage comparison and zero financial authority."""

    from apex.application.multi_strategy_shadow import BuildEnrichedShadowMarketCohort
    from apex.domain.multi_strategy import (
        CompatiblePredictionEvidence,
        EnrichedShadowMarket,
        ShadowMarketCohortConfiguration,
        compare_prediction_evidence,
    )

    now = datetime(2031, 1, 1, tzinfo=UTC)
    rule_hash = ContentHash(content_digest({"demo": "multi-strategy-rule-v1"}))

    def market(ordinal: int) -> EnrichedShadowMarket:
        return EnrichedShadowMarket(
            market_snapshot_id=f"multi-strategy-demo-market-snapshot-{ordinal}",
            provider="KALSHI",
            market_ticker=f"DEMO-MARKET-{ordinal}",
            event_ticker=f"DEMO-EVENT-{ordinal}",
            series_ticker=f"DEMO-SERIES-{ordinal}",
            rule_content_hash=rule_hash,
            lifecycle_state="open",
            observed_at=now,
            available_at=now,
            close_at=now + timedelta(days=1),
            market_lineage_hash=ContentHash(content_digest({"market": ordinal})),
            event_lineage_hash=ContentHash(content_digest({"event": ordinal})),
            series_lineage_hash=ContentHash(content_digest({"series": ordinal})),
            fee_lineage_hash=ContentHash(content_digest({"fee": ordinal})),
        )

    markets = tuple(market(ordinal) for ordinal in range(1, 4))
    configuration = ShadowMarketCohortConfiguration(selected_market_count=3)
    cohort = BuildEnrichedShadowMarketCohort.build(
        campaign_id="multi-strategy-demo-campaign",
        cycle_key="multi-strategy-demo-v1",
        configuration=configuration,
        candidates=markets,
        decision_at=now,
    )
    values = tuple(
        CompatiblePredictionEvidence(
            prediction_id=f"multi-strategy-demo-{strategy_id}",
            strategy_id=strategy_id,
            strategy_version="v1",
            market_ticker=markets[0].market_ticker,
            event_ticker=markets[0].event_ticker,
            decision_at=now,
            market_yes_probability=Decimal("0.50"),
            point_yes_probability=point,
            conservative_yes_probability=conservative,
            market_evidence_id=markets[0].market_snapshot_id,
            feature_snapshot_id=None,
            configuration_version="demo-v1",
            rule_content_hash=markets[0].rule_content_hash,
        )
        for strategy_id, point, conservative in (
            ("market-consensus-baseline-v1", Decimal("0.50"), Decimal("0.50")),
            ("kalshi-orderbook-microstructure-v1", Decimal("0.56"), Decimal("0.53")),
            ("kalshi-market-dynamics-v1", Decimal("0.55"), Decimal("0.52")),
        )
    )
    comparison = compare_prediction_evidence(cycle_id="multi-strategy-demo-cycle", values=values)
    return {
        "shared_enriched_market_cohort": tuple(item.market_ticker for item in cohort.selected),
        "strategies": tuple(item.strategy_id for item in values),
        "comparison_state": comparison.state,
        "no_probability_ensemble": True,
        "same_direction_duplicate_grouping": "DEMONSTRATED_BY_PRODUCTION_SERVICE",
        "opposite_direction_conflict": "REJECT_BOTH_DIRECTIONAL_CONFLICT",
        "deterministic_replay": cohort.cohort_id
        == BuildEnrichedShadowMarketCohort.build(
            campaign_id="multi-strategy-demo-campaign",
            cycle_key="multi-strategy-demo-v1",
            configuration=configuration,
            candidates=markets,
            decision_at=now,
        ).cohort_id,
        "financial_counts": {
            "reservations": 0,
            "orders": 0,
            "positions": 0,
            "settlements": 0,
            "ledger_change": 0,
        },
        "authority": "SHADOW / HYPOTHETICAL — NO CAPITAL RESERVED",
    }


def paper_portfolio_status(campaign_id: str) -> dict[str, object]:
    from apex.adapters.postgres import postgres_paper_portfolio_store

    _, engine = _autopilot_store()
    try:
        return PaperPortfolioOperations(postgres_paper_portfolio_store(cast(Any, engine))).status(
            campaign_id
        )
    finally:
        engine.dispose()


def paper_mark_positions(campaign_id: str, orderbook_run_id: str) -> tuple[object, ...]:
    from apex.adapters.postgres import postgres_paper_portfolio_store

    _, engine = _autopilot_store()
    try:
        return PaperPortfolioOperations(
            postgres_paper_portfolio_store(cast(Any, engine))
        ).mark_positions(
            PaperPositionMarkCommand(
                campaign_id=campaign_id,
                evidence_id=orderbook_run_id,
                recorded_at=datetime.now(tz=UTC),
            )
        )
    finally:
        engine.dispose()


def paper_settle_position(position_id: str, settlement_fact_id: str) -> str:
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        position,
        postgres_paper_portfolio_store,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        with cast(Any, engine).connect() as connection:
            campaign_id = connection.scalar(
                position.select()
                .with_only_columns(position.c.campaign_id)
                .where(position.c.position_id == position_id)
            )
        if campaign_id is None:
            raise ValueError("paper Position was not found")
        return PaperPortfolioOperations(postgres_paper_portfolio_store(engine)).settle_position(
            PaperPositionSettlementCommand(
                campaign_id=str(campaign_id),
                position_id=position_id,
                settlement_fact_id=settlement_fact_id,
                recorded_at=datetime.now(tz=UTC),
            )
        )
    finally:
        engine.dispose()


def _demo_paper_execution_policy() -> PaperExecutionPolicy:
    payload = {"paper-policy": "fixture-v1"}
    return PaperExecutionPolicy(
        policy_version_id="paper-portfolio-demo-execution-policy-v1",
        execution_model=PaperExecutionModel.IMMEDIATE_CROSS_V1,
        maximum_evidence_age_seconds=30,
        maximum_spread=Decimal("0.02"),
        maximum_orderbook_depth=5,
        maximum_visible_size_participation=RiskFraction("1"),
        slippage_reserve_per_contract=Money("0"),
        fee_model_version="fixture-autopilot-fee-v1",
        time_in_force_seconds=30,
        unfilled_behavior=PaperUnfilledBehavior.EXPIRE,
        configuration_version="fixture-paper-v1",
        configuration_hash=ContentHash(content_digest(payload)),
    )


def _demo_paper_settlement_fact(
    row: dict[str, object],
    *,
    classification: SettlementOutcome,
    recorded_at: datetime,
) -> ClassifiedRuleBoundSettlementFact:
    value = Decimal("1") if classification is SettlementOutcome.YES else Decimal("0")
    value_text = str(int(value))
    rule_hash = ContentHash(str(row["rule_content_hash"]))
    resolution_source = str(row["resolution_source"])
    raw_payload = (
        f"resolution:{classification.value};settlement_value={value_text};"
        f"instrument={row['instrument_id']};rule={row['rule_version_id']};"
        f"rule_hash={rule_hash.value};source={resolution_source}"
    )
    raw_payload_hash = ContentHash(sha256(raw_payload.encode("utf-8")).hexdigest())
    source_event_at = recorded_at - timedelta(seconds=1)
    evidence = {
        "fixture_id": "paper-portfolio-demo-settlement",
        "schema_version": "2",
        "source_id": f"paper-portfolio-demo-{classification.value.lower()}-source",
        "instrument_id": str(row["instrument_id"]),
        "rule_version_id": str(row["rule_version_id"]),
        "rule_content_hash": rule_hash.value,
        "resolution_source": resolution_source,
        "outcome_classification": classification,
        "source_event_at": source_event_at,
        "available_at": recorded_at,
        "settlement_value": value_text,
        "raw_payload": raw_payload,
        "raw_payload_hash": raw_payload_hash.value,
    }
    return ClassifiedRuleBoundSettlementFact(
        source_id=str(evidence["source_id"]),
        instrument_id=str(row["instrument_id"]),
        rule_version_id=str(row["rule_version_id"]),
        source_event_at=source_event_at,
        available_at=recorded_at,
        settlement_value=value,
        content_hash=ContentHash(content_digest(evidence)),
        fixture_id=str(evidence["fixture_id"]),
        schema_version="2",
        settlement_value_text=value_text,
        resolution_source=resolution_source,
        rule_content_hash=rule_hash,
        outcome_classification=classification,
        raw_payload=raw_payload,
        raw_payload_hash=raw_payload_hash,
    )


def _demo_autopilot_policy(campaign_id: str) -> CampaignPolicy:
    policy_version_id = f"{campaign_id}-campaign-policy-v1"
    payload = {
        "policy_version_id": policy_version_id,
        "profile": "MOONSHOT_SHADOW_DEMO",
        "requested_risk_fraction": "1",
        "max_trade_loss_fraction": "1",
        "max_open_loss_fraction": "1",
        "min_synthetic_edge": "0",
        "max_spread": "1",
        "max_capital_lockup_seconds": 86_400,
        "campaign_id": campaign_id,
    }
    return CampaignPolicy(
        policy_version_id=policy_version_id,
        profile="MOONSHOT_SHADOW_DEMO",
        requested_risk_fraction="1",
        max_trade_loss_fraction="1",
        max_open_loss_fraction="1",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=86_400,
        content_hash=ContentHash(content_digest(payload)),
    )


def autopilot_core_demo() -> dict[str, object]:
    """Run the bounded offline demonstration without provider or trade access."""

    from apex.adapters.postgres import postgres_uow_factory

    campaign_id = "autopilot-core-demo-v2"
    portfolio_policy_version_id = f"{campaign_id}-portfolio-policy-v1"
    requested_at = datetime(2026, 7, 21, 18, 0, tzinfo=UTC)
    store, engine = _autopilot_store()
    try:
        campaign_policy = _demo_autopilot_policy(campaign_id)
        uow_factory = cast(
            Callable[[], AuthorizationUnitOfWork], postgres_uow_factory(cast(Any, engine))
        )
        DurableAuthorizationService(uow_factory).initialize_campaign(
            CampaignInitializationCommand(
                campaign_id=campaign_id,
                contribution=Money("1000"),
                authorized_loss=Money("500"),
                policy=campaign_policy,
                occurred_at=requested_at,
            )
        )
        portfolio_hash_payload = {
            "campaign_id": campaign_id,
            "policy_version_id": portfolio_policy_version_id,
            "maximum_active_risk_fraction": "0.5",
            "maximum_per_opportunity_risk_fraction": "0.2",
            "maximum_per_strategy_risk_fraction": "0.5",
            "maximum_per_correlation_bucket_risk_fraction": "0.5",
            "maximum_simultaneous_reservations": 2,
            "maximum_simultaneous_open_positions": 2,
            "maximum_capital_lockup_seconds": 3600,
            "minimum_conservative_net_expected_value": "0",
            "minimum_evidence_tier": EvidenceTier.RESEARCH,
            "allow_profit_compounding": False,
        }
        store.ensure_portfolio_policy(
            CampaignPortfolioPolicy(
                policy_version_id=portfolio_policy_version_id,
                campaign_id=campaign_id,
                maximum_active_risk_fraction="0.5",
                maximum_per_opportunity_risk_fraction="0.2",
                maximum_per_strategy_risk_fraction="0.5",
                maximum_per_correlation_bucket_risk_fraction="0.5",
                maximum_simultaneous_reservations=2,
                maximum_simultaneous_open_positions=2,
                maximum_capital_lockup_seconds=3600,
                minimum_conservative_net_expected_value="0",
                minimum_evidence_tier=EvidenceTier.RESEARCH,
                allow_profit_compounding=False,
                configuration_hash=ContentHash(content_digest(portfolio_hash_payload)),
            ),
            recorded_at=requested_at,
        )
        registry = fixture_autopilot_registry()
        result = RunAutopilotShadowCycle(store, registry).run(
            AutopilotCycleCommand(
                campaign_id=campaign_id,
                idempotency_key="autopilot-core-demo-cycle-v1",
                maximum_strategies=2,
                maximum_proposals=3,
                deadline_at=requested_at + timedelta(seconds=30),
                operator_note="bounded fixture-only Autopilot Core demonstration",
                requested_at=requested_at,
            )
        )
        store.control(
            campaign_id=campaign_id,
            action="PAUSE",
            reason="demonstrate pause control",
            idempotency_key="autopilot-core-demo-pause-v1",
            occurred_at=requested_at + timedelta(seconds=1),
        )
        try:
            RunAutopilotShadowCycle(store, registry).run(
                AutopilotCycleCommand(
                    campaign_id=campaign_id,
                    idempotency_key="autopilot-core-demo-paused-cycle-v1",
                    maximum_strategies=2,
                    maximum_proposals=3,
                    deadline_at=requested_at + timedelta(seconds=32),
                    operator_note="must be blocked while paused",
                    requested_at=requested_at + timedelta(seconds=2),
                )
            )
        except CampaignControlError as error:
            pause_block = str(error)
        else:  # pragma: no cover - defensive proof for the product demo
            raise RuntimeError("paused Autopilot cycle was not blocked")
        store.control(
            campaign_id=campaign_id,
            action="KILL",
            reason="demonstrate kill control",
            idempotency_key="autopilot-core-demo-kill-v1",
            occurred_at=requested_at + timedelta(seconds=3),
        )
        try:
            RunAutopilotShadowCycle(store, registry).run(
                AutopilotCycleCommand(
                    campaign_id=campaign_id,
                    idempotency_key="autopilot-core-demo-killed-cycle-v1",
                    maximum_strategies=2,
                    maximum_proposals=3,
                    deadline_at=requested_at + timedelta(seconds=34),
                    operator_note="must be blocked while killed",
                    requested_at=requested_at + timedelta(seconds=4),
                )
            )
        except CampaignControlError as error:
            kill_block = str(error)
        else:  # pragma: no cover - defensive proof for the product demo
            raise RuntimeError("killed Autopilot cycle was not blocked")
        status = store.status(campaign_id)
        assessment_evidence = store.cycle_assessment_evidence(result.cycle_id)
        portfolio_snapshot = cast(CampaignPortfolioSnapshot, status["portfolio_snapshot"])
        return {
            "cycle_id": result.cycle_id,
            "cycle_state": result.state,
            "invocation_count": result.invocation_count,
            "approved_count": result.approved_count,
            "rejected_count": result.rejected_count,
            "assessment_validity": tuple(item["validity"] for item in assessment_evidence),
            "assessment_reasons": tuple(item["reason_codes"] for item in assessment_evidence),
            "pause_block": pause_block,
            "kill_block": kill_block,
            "financial_state_unchanged": (
                portfolio_snapshot.current_reservation_risk == 0
                and portfolio_snapshot.current_open_position_risk == 0
            ),
            "status": status,
        }
    finally:
        engine.dispose()


def paper_portfolio_demo() -> dict[str, object]:
    """Run the bounded local PAPER product loop with fixture-only evidence."""

    from apex.adapters.postgres import (
        durable_order,
        paper_execution,
        position,
        postgres_paper_portfolio_store,
        postgres_uow_factory,
    )

    campaign_id = "paper-portfolio-demo-v4"
    requested_at = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
    store, engine = _autopilot_store()
    try:
        campaign_policy = _demo_autopilot_policy(campaign_id)
        uow_factory = cast(
            Callable[[], AuthorizationUnitOfWork], postgres_uow_factory(cast(Any, engine))
        )
        DurableAuthorizationService(uow_factory).initialize_campaign(
            CampaignInitializationCommand(
                campaign_id=campaign_id,
                contribution=Money("1000"),
                authorized_loss=Money("500"),
                policy=campaign_policy,
                occurred_at=requested_at,
            )
        )
        portfolio_payload = {"campaign_id": campaign_id, "portfolio": "paper-demo-v1"}
        store.ensure_portfolio_policy(
            CampaignPortfolioPolicy(
                policy_version_id=f"{campaign_id}-portfolio-policy-v1",
                campaign_id=campaign_id,
                maximum_active_risk_fraction="1",
                maximum_per_opportunity_risk_fraction="0.2",
                maximum_per_strategy_risk_fraction="1",
                maximum_per_correlation_bucket_risk_fraction="1",
                maximum_simultaneous_reservations=4,
                maximum_simultaneous_open_positions=4,
                maximum_capital_lockup_seconds=3600,
                minimum_conservative_net_expected_value="0",
                minimum_evidence_tier=EvidenceTier.RESEARCH,
                allow_profit_compounding=False,
                configuration_hash=ContentHash(content_digest(portfolio_payload)),
            ),
            recorded_at=requested_at,
        )
        paper_store = postgres_paper_portfolio_store(cast(Any, engine))
        result = RunAutopilotPaperCycle(store, fixture_paper_portfolio_registry(), paper_store).run(
            PaperCycleCommand(
                cycle=AutopilotCycleCommand(
                    campaign_id=campaign_id,
                    idempotency_key="paper-portfolio-demo-cycle-v1",
                    maximum_strategies=2,
                    maximum_proposals=4,
                    deadline_at=requested_at + timedelta(seconds=30),
                    operator_note="bounded fixture PAPER portfolio demo",
                    requested_at=requested_at,
                    authority=AutopilotCycleAuthority.PAPER,
                ),
                execution_policy=_demo_paper_execution_policy(),
            )
        )
        with cast(Any, engine).connect() as connection:
            full_evidence_id = str(
                connection.scalar(
                    paper_execution.select()
                    .with_only_columns(paper_execution.c.evidence_id)
                    .where(
                        paper_execution.c.campaign_id == campaign_id,
                        paper_execution.c.state == "FULL_FILL",
                    )
                )
            )
            full_position = dict(
                connection.execute(
                    position.select()
                    .join(
                        durable_order, durable_order.c.order_id == position.c.originating_order_id
                    )
                    .with_only_columns(
                        position,
                        durable_order.c.state.label("order_state"),
                        durable_order.c.proposal_id,
                    )
                    .where(
                        position.c.campaign_id == campaign_id,
                        durable_order.c.state == "FILLED",
                    )
                )
                .mappings()
                .one()
            )
            partial_position = dict(
                connection.execute(
                    position.select()
                    .join(
                        durable_order, durable_order.c.order_id == position.c.originating_order_id
                    )
                    .with_only_columns(
                        position,
                        durable_order.c.proposal_id,
                    )
                    .where(
                        position.c.campaign_id == campaign_id,
                        durable_order.c.state == "CANCELED",
                    )
                )
                .mappings()
                .one()
            )
            from apex.adapters.postgres import opportunity_proposal

            full_rule = dict(
                connection.execute(
                    opportunity_proposal.select().where(
                        opportunity_proposal.c.proposal_id == full_position["proposal_id"]
                    )
                )
                .mappings()
                .one()
            )
            partial_rule = dict(
                connection.execute(
                    opportunity_proposal.select().where(
                        opportunity_proposal.c.proposal_id == partial_position["proposal_id"]
                    )
                )
                .mappings()
                .one()
            )
        marks = PaperPortfolioOperations(paper_store).mark_positions(
            PaperPositionMarkCommand(
                campaign_id=campaign_id,
                evidence_id=full_evidence_id,
                recorded_at=requested_at + timedelta(seconds=2),
            )
        )
        full_fact = _demo_paper_settlement_fact(
            {**full_position, **full_rule},
            classification=SettlementOutcome.YES,
            recorded_at=requested_at + timedelta(hours=1),
        )
        full_settlement_id = paper_store.settle_position(
            campaign_id=campaign_id,
            position_id=str(full_position["position_id"]),
            fact=full_fact,
            void_term_content_hash=None,
            recorded_at=requested_at + timedelta(hours=1),
        )
        store.control(
            campaign_id=campaign_id,
            action="PAUSE",
            reason="fixture pause control proof",
            idempotency_key="paper-portfolio-demo-pause-v1",
            occurred_at=requested_at + timedelta(hours=1, seconds=1),
        )
        paused_blocked = False
        try:
            RunAutopilotPaperCycle(store, fixture_paper_portfolio_registry(), paper_store).run(
                PaperCycleCommand(
                    cycle=AutopilotCycleCommand(
                        campaign_id=campaign_id,
                        idempotency_key="paper-portfolio-demo-paused-v1",
                        maximum_strategies=1,
                        maximum_proposals=1,
                        deadline_at=requested_at + timedelta(hours=1, seconds=31),
                        operator_note="must be blocked",
                        requested_at=requested_at + timedelta(hours=1, seconds=1),
                        authority=AutopilotCycleAuthority.PAPER,
                    ),
                    execution_policy=_demo_paper_execution_policy(),
                )
            )
        except CampaignControlError:
            paused_blocked = True
        store.control(
            campaign_id=campaign_id,
            action="RESUME",
            reason="fixture resume control proof",
            idempotency_key="paper-portfolio-demo-resume-v1",
            occurred_at=requested_at + timedelta(hours=1, seconds=2),
        )
        store.control(
            campaign_id=campaign_id,
            action="KILL",
            reason="fixture kill control proof",
            idempotency_key="paper-portfolio-demo-kill-v1",
            occurred_at=requested_at + timedelta(hours=1, seconds=3),
        )
        killed_blocked = False
        try:
            RunAutopilotPaperCycle(store, fixture_paper_portfolio_registry(), paper_store).run(
                PaperCycleCommand(
                    cycle=AutopilotCycleCommand(
                        campaign_id=campaign_id,
                        idempotency_key="paper-portfolio-demo-killed-v1",
                        maximum_strategies=1,
                        maximum_proposals=1,
                        deadline_at=requested_at + timedelta(hours=1, seconds=33),
                        operator_note="must be blocked",
                        requested_at=requested_at + timedelta(hours=1, seconds=3),
                        authority=AutopilotCycleAuthority.PAPER,
                    ),
                    execution_policy=_demo_paper_execution_policy(),
                )
            )
        except CampaignControlError:
            killed_blocked = True
        partial_fact = _demo_paper_settlement_fact(
            {**partial_position, **partial_rule},
            classification=SettlementOutcome.NO,
            recorded_at=requested_at + timedelta(hours=1, seconds=4),
        )
        partial_settlement_id = paper_store.settle_position(
            campaign_id=campaign_id,
            position_id=str(partial_position["position_id"]),
            fact=partial_fact,
            void_term_content_hash=None,
            recorded_at=requested_at + timedelta(hours=1, seconds=4),
        )
        status = PaperPortfolioOperations(paper_store).status(campaign_id)
        return {
            "campaign_id": campaign_id,
            "cycle_state": result.state,
            "approved": result.approved_count,
            "rejected": result.rejected_count,
            "mark_count": len(marks),
            "full_settlement_id": full_settlement_id,
            "partial_settlement_after_kill_id": partial_settlement_id,
            "pause_blocked": paused_blocked,
            "kill_blocked": killed_blocked,
            "existing_void_path": "UNCHANGED",
            "portfolio_status": status,
        }
    finally:
        engine.dispose()


def strategy_shadow_demo() -> dict[str, object]:
    """Exercise the non-financial promotion and forward-scoring product loop.

    The demo retains one local Kalshi-shaped public response and a separate
    deterministic settlement fact.  They are test evidence only: the demo
    never calls a provider endpoint and never creates a reservation, Order,
    Position, execution event, or ledger posting from shadowing.
    """

    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_operator_console_store,
        postgres_public_market_discovery_store,
        postgres_strategy_shadow_store,
    )
    from apex.application.autopilot import StrategyAuthorityState
    from apex.application.operator_console import (
        CreateMoonshotCampaign,
        CreateMoonshotCampaignCommand,
        OperatorMode,
    )

    campaign_id = "strategy-shadow-demo-v1"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    market_document: dict[str, object] = {
        "markets": [
            {
                "ticker": "KXSHADOW-DEMO",
                "event_ticker": "KXSHADOW-DEMO-EVENT",
                "title": "Deterministic forward-shadow demonstration",
                "subtitle": "Local retained evidence only",
                "status": "open",
                "open_time": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                "close_time": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                "latest_expiration_time": (now + timedelta(hours=1, minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
                "yes_bid_dollars": "0.54",
                "yes_ask_dollars": "0.56",
                "no_bid_dollars": "0.44",
                "no_ask_dollars": "0.46",
                "yes_bid_size_fp": "10",
                "yes_ask_size_fp": "10",
                "volume_fp": "0",
                "volume_24h_fp": "0",
                "open_interest_fp": "0",
                "liquidity_dollars": "0",
                "rules_primary": (
                    "This local demonstration contract resolves by its stated condition."
                ),
                "rules_secondary": None,
            }
        ]
    }

    class _DemoPublicMarketClient:
        def fetch(
            self,
            *,
            status: str,
            page_size: int,
            cursor: str | None,
            timeout_seconds: int,
            exclude_multivariate: bool,
        ) -> CapturedPublicMarketPage:
            if status != "open" or cursor is not None or not exclude_multivariate:
                raise ValueError("demo public market request is outside its fixed bounds")
            raw_payload = dumps(market_document, separators=(",", ":")).encode("utf-8")
            return CapturedPublicMarketPage(
                endpoint_identity="KALSHI_GET_MARKETS",
                request_cursor=None,
                received_at=now,
                http_status=200,
                response_content_type="application/json",
                raw_payload=raw_payload,
                response_cursor=None,
                document=market_document,
            )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        console = postgres_operator_console_store(engine)
        CreateMoonshotCampaign(console).create(
            CreateMoonshotCampaignCommand(
                campaign_id=campaign_id,
                name="Strategy Shadow demonstration",
                initial_capital=Money("1000"),
                maximum_authorized_loss=Money("500"),
                starts_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(days=1),
                operating_mode=OperatorMode.SHADOW,
                maximum_active_risk_fraction=RiskFraction("0.5"),
                maximum_per_opportunity_risk_fraction=RiskFraction("0.1"),
                maximum_per_strategy_risk_fraction=RiskFraction("0.25"),
                maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.25"),
                maximum_simultaneous_reservations=4,
                maximum_simultaneous_positions=4,
                maximum_capital_lockup_seconds=3600,
                minimum_conservative_expected_value=Money("0"),
                minimum_evidence_tier=EvidenceTier.RESEARCH,
                allow_profit_compounding=False,
                operator_note="deterministic non-financial strategy-shadow demonstration",
                idempotency_key="strategy-shadow-demo-campaign-v1",
            )
        )
        discovery = postgres_public_market_discovery_store(engine)
        summary = DiscoverPublicPredictionMarkets(
            client=_DemoPublicMarketClient(),
            normalizer=KalshiPublicMarketNormalizer(),
            store=discovery,
            clock=lambda: now,
        ).discover(
            PublicMarketScanRequest(
                run_key="strategy-shadow-demo-public-market-v1",
                page_size=1,
                maximum_pages=1,
                timeout_seconds=1,
            )
        )
        snapshot_id = discovery.market_snapshot_ids_for_run(summary.ingestion_run_id, limit=1)[0]
        shadow_store = postgres_strategy_shadow_store(engine)
        initial_authority = shadow_store.current_authority(OPERATOR_THESIS_STRATEGY_ID)
        before_financial = shadow_store.demo_financial_counts(campaign_id)

        positive = submit_operator_thesis(
            campaign_id=campaign_id,
            market_snapshot_id=snapshot_id,
            point_probabilities=(("NO", "0.20"), ("YES", "0.80")),
            conservative_probabilities=(("NO", "0.30"), ("YES", "0.70")),
            resolution_at=now + timedelta(hours=2),
            requested_risk_fraction="0.05",
            thesis_summary="A retained local market snapshot supports a bounded forward thesis.",
            evidence_references=("demo-retained-public-market",),
            correlation_buckets=("demo",),
            operator_note="No payout, fee, quote, outcome, or execution term was supplied.",
            idempotency_key="strategy-shadow-demo-positive-v1",
        )
        replay = submit_operator_thesis(
            campaign_id=campaign_id,
            market_snapshot_id=snapshot_id,
            point_probabilities=(("NO", "0.20"), ("YES", "0.80")),
            conservative_probabilities=(("NO", "0.30"), ("YES", "0.70")),
            resolution_at=now + timedelta(hours=2),
            requested_risk_fraction="0.05",
            thesis_summary="A retained local market snapshot supports a bounded forward thesis.",
            evidence_references=("demo-retained-public-market",),
            correlation_buckets=("demo",),
            operator_note="No payout, fee, quote, outcome, or execution term was supplied.",
            idempotency_key="strategy-shadow-demo-positive-v1",
        )
        rejected = submit_operator_thesis(
            campaign_id=campaign_id,
            market_snapshot_id=snapshot_id,
            point_probabilities=(("NO", "0.55"), ("YES", "0.45")),
            conservative_probabilities=(("NO", "0.56"), ("YES", "0.44")),
            resolution_at=now + timedelta(hours=2),
            requested_risk_fraction="0.05",
            thesis_summary="This retained thesis intentionally has nonpositive conservative value.",
            evidence_references=("demo-retained-public-market",),
            correlation_buckets=("demo",),
            operator_note="Expected explicit no-proposal result.",
            idempotency_key="strategy-shadow-demo-rejected-v1",
        )
        market = shadow_store.retained_market(snapshot_id)
        baseline = build_market_consensus_baseline_prediction(
            market=market, campaign_id=campaign_id, decision_at=now
        )
        shadow_store.persist_prediction(baseline)
        positive_prediction = cast(Any, positive["prediction"])
        fact_id = "strategy-shadow-demo-settlement-fact-v1"
        shadow_store.retain_demo_settlement_fact(
            campaign_id=campaign_id,
            settlement_fact_id_value=fact_id,
            instrument_id=positive_prediction.instrument_id,
            rule_content_hash=market.rule_content_hash.value,
            recorded_at=now + timedelta(seconds=5),
        )
        evaluation = evaluate_shadow_prediction(
            prediction_id=positive_prediction.prediction_id,
            settlement_fact_id_value=fact_id,
            operator_note="deterministic retained outcome evaluation",
            idempotency_key="strategy-shadow-demo-evaluation-v1",
        )
        scorecard = shadow_store.scorecard(
            strategy_id=OPERATOR_THESIS_STRATEGY_ID, campaign_id=campaign_id
        )
        shadow_store.change_authority(
            StrategyAuthorityCommand(
                strategy_id=OPERATOR_THESIS_STRATEGY_ID,
                requested_authority=StrategyAuthorityState.PAUSED,
                operator_reason="demonstrate explicit pause rejection",
                evidence_review_id="strategy-shadow-demo-review-v1",
                idempotency_key="strategy-shadow-demo-pause-v1",
                confirmation_text=None,
                requested_at=now + timedelta(seconds=6),
            )
        )
        try:
            submit_operator_thesis(
                campaign_id=campaign_id,
                market_snapshot_id=snapshot_id,
                point_probabilities=(("NO", "0.20"), ("YES", "0.80")),
                conservative_probabilities=(("NO", "0.30"), ("YES", "0.70")),
                resolution_at=now + timedelta(hours=2),
                requested_risk_fraction="0.05",
                thesis_summary="This must not run while the strategy is paused.",
                evidence_references=("demo-retained-public-market",),
                correlation_buckets=("demo",),
                operator_note="expected pause rejection",
                idempotency_key="strategy-shadow-demo-paused-v1",
            )
        except AutopilotError as error:
            paused_rejection = str(error)
        else:  # pragma: no cover - defensive demo proof
            raise RuntimeError("paused Operator Thesis unexpectedly accepted a new thesis")
        after_financial = shadow_store.demo_financial_counts(campaign_id)
        weather = shadow_store.current_authority("gfs-weather-research")
        rejected_structures = tuple(
            cast(
                StrategyAuthorityState,
                shadow_store.current_authority(strategy_id)["authority"],
            ).value
            for strategy_id in ("kalshi-same-market-complement", "kalshi-threshold-ladder")
        )
        return {
            "campaign_id": campaign_id,
            "operator_thesis_initial_authority": initial_authority["authority"],
            "market_consensus_authority": shadow_store.current_authority(
                MARKET_CONSENSUS_BASELINE_STRATEGY_ID
            )["authority"],
            "positive_proposal_id": positive_prediction.proposal_id,
            "rejected_thesis_proposal_id": cast(Any, rejected["prediction"]).proposal_id,
            "idempotent_replay": bool(replay["idempotent_replay"]),
            "resolved_prediction_id": positive_prediction.prediction_id,
            "evaluation_id": cast(Any, evaluation["evaluation"]).evaluation_id,
            "brier_score": scorecard["brier_score"],
            "market_baseline_brier_score": scorecard["market_baseline_brier_score"],
            "calibration_buckets": scorecard["calibration_buckets"],
            "paused_strategy_rejection": paused_rejection,
            "weather_authority": weather["authority"],
            "rejected_structural_authorities": rejected_structures,
            "financial_counts_before": before_financial,
            "financial_counts_after": after_financial,
            "shadow_financial_state_unchanged": before_financial
            == after_financial
            == {
                "reservations": 0,
                "orders": 0,
                "positions": 0,
                "ledger_journals": 1,
            },
            "provider_order_calls": 0,
        }
    finally:
        engine.dispose()


def orderbook_microstructure_shadow_demo() -> dict[str, object]:
    """Exercise fixed public-orderbook features without network or financial writes."""

    from apex.application.orderbook_microstructure_shadow import evaluate_retained_microstructure
    from apex.application.strategy_shadow import RetainedMarketEvidence
    from apex.domain.orderbook_microstructure import (
        MicrostructureMarketEvidence,
        MicrostructureOrderbookEvidence,
        OrderbookMicrostructureStrategyConfig,
        evaluate_microstructure_features,
    )
    from apex.domain.public_complement import (
        FEE_MODEL_IDENTITY,
        MAKER_COEFFICIENT,
        ROUNDING_IDENTITY,
        SCHEDULE_EFFECTIVE_DATE,
        SCHEDULE_IDENTITY,
        TAKER_COEFFICIENT,
        OrderbookSide,
        PublicOrderbookLevel,
        PublicOrderbookSnapshot,
        SeriesFeeSnapshot,
    )
    from apex.domain.public_market import PublicMarketProvider
    from apex.domain.values import ContentHash

    now = datetime.now(tz=UTC).replace(microsecond=0)
    source_hash = ContentHash("a" * 64)

    def observation(
        ordinal: int, *, bid_quantity: str, ask_quantity: str
    ) -> MicrostructureOrderbookEvidence:
        observed_at = now - timedelta(seconds=(3 - ordinal) * 10)
        book_id = f"microstructure-demo-book-{ordinal}"
        book = PublicOrderbookSnapshot(
            orderbook_snapshot_id=book_id,
            ingestion_run_id=f"microstructure-demo-run-{ordinal}",
            raw_response_id=f"microstructure-demo-raw-{ordinal}",
            market_snapshot_id="strategy-shadow-demo-market-1",
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker="KX-MICROSTRUCTURE-DEMO",
            provider_event_ticker="KX-MICROSTRUCTURE-DEMO-EVENT",
            series_ticker="KX-MICROSTRUCTURE-DEMO-SERIES",
            observed_at=observed_at,
            maximum_depth=5,
            yes_bid_level_count=1,
            no_bid_level_count=1,
            data_quality_state="VALID",
            canonical_source_hash=source_hash,
        )
        fee = SeriesFeeSnapshot(
            fee_snapshot_id=f"microstructure-demo-fee-{ordinal}",
            ingestion_run_id=f"microstructure-demo-run-{ordinal}",
            raw_response_id=f"microstructure-demo-fee-raw-{ordinal}",
            provider=PublicMarketProvider.KALSHI,
            series_ticker="KX-MICROSTRUCTURE-DEMO-SERIES",
            observed_at=observed_at,
            fee_type="quadratic",
            fee_multiplier=Decimal(1),
            effective_at=None,
            fee_model_identity=FEE_MODEL_IDENTITY,
            taker_coefficient=TAKER_COEFFICIENT,
            maker_coefficient=MAKER_COEFFICIENT,
            rounding_identity=ROUNDING_IDENTITY,
            schedule_identity=SCHEDULE_IDENTITY,
            schedule_effective_date=SCHEDULE_EFFECTIVE_DATE,
            source_hash=source_hash,
        )
        return MicrostructureOrderbookEvidence(
            book,
            (
                PublicOrderbookLevel(
                    f"{book_id}-yes",
                    book_id,
                    OrderbookSide.YES_BID,
                    0,
                    Decimal("0.50"),
                    Decimal(bid_quantity),
                    1,
                    source_hash,
                ),
                PublicOrderbookLevel(
                    f"{book_id}-no",
                    book_id,
                    OrderbookSide.NO_BID,
                    0,
                    Decimal("0.46"),
                    Decimal(ask_quantity),
                    1,
                    source_hash,
                ),
            ),
            fee,
            source_hash,
        )

    def evidence(
        values: tuple[MicrostructureOrderbookEvidence, ...],
    ) -> MicrostructureMarketEvidence:
        return MicrostructureMarketEvidence(
            market_snapshot_id="strategy-shadow-demo-market-1",
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker="KX-MICROSTRUCTURE-DEMO",
            provider_event_ticker="KX-MICROSTRUCTURE-DEMO-EVENT",
            series_ticker="KX-MICROSTRUCTURE-DEMO-SERIES",
            observed_at=now - timedelta(seconds=30),
            available_at=now - timedelta(seconds=29),
            close_at=now + timedelta(hours=1),
            active=True,
            rule_content_hash=source_hash,
            observations=values,
        )

    positive_feature = evaluate_microstructure_features(
        market=evidence(
            (
                observation(0, bid_quantity="8", ask_quantity="4"),
                observation(1, bid_quantity="8", ask_quantity="4"),
                observation(2, bid_quantity="8", ask_quantity="4"),
            )
        ),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=now,
    )
    negative_feature = evaluate_microstructure_features(
        market=evidence(
            (
                observation(0, bid_quantity="4", ask_quantity="8"),
                observation(1, bid_quantity="4", ask_quantity="8"),
                observation(2, bid_quantity="4", ask_quantity="8"),
            )
        ),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=now,
    )
    stale_feature = evaluate_microstructure_features(
        market=evidence(
            (
                observation(0, bid_quantity="8", ask_quantity="4"),
                observation(1, bid_quantity="8", ask_quantity="4"),
                observation(2, bid_quantity="8", ask_quantity="4"),
            )
        ),
        configuration=OrderbookMicrostructureStrategyConfig(
            maximum_observation_age=timedelta(seconds=5)
        ),
        decision_at=now,
    )
    one_sided = observation(2, bid_quantity="8", ask_quantity="4")
    one_sided_feature = evaluate_microstructure_features(
        market=evidence(
            (
                observation(0, bid_quantity="8", ask_quantity="4"),
                observation(1, bid_quantity="8", ask_quantity="4"),
                MicrostructureOrderbookEvidence(
                    one_sided.orderbook, one_sided.levels[:1], one_sided.fee, source_hash
                ),
            )
        ),
        configuration=OrderbookMicrostructureStrategyConfig(),
        decision_at=now,
    )
    market = RetainedMarketEvidence(
        snapshot_id="strategy-shadow-demo-market-1",
        provider="KALSHI",
        provider_market_ticker="KX-MICROSTRUCTURE-DEMO",
        provider_event_ticker="KX-MICROSTRUCTURE-DEMO-EVENT",
        observed_at=now - timedelta(seconds=30),
        available_at=now - timedelta(seconds=29),
        close_at=now + timedelta(hours=1),
        rule_text="Synthetic retained public orderbook demo contract rule.",
        rule_content_hash=source_hash,
        yes_bid=Decimal("0.50"),
        yes_ask=Decimal("0.54"),
        no_ask=Decimal("0.50"),
        visible_yes_quantity=Decimal("8"),
        fee_model_version=FEE_MODEL_IDENTITY,
        expected_fee=Decimal(0),
    )
    default = OrderbookMicrostructureStrategyConfig()
    no_proposal = evaluate_retained_microstructure(
        campaign_id="strategy-shadow-demo-v1",
        market=market,
        feature=positive_feature,
        configuration=default,
        decision_at=now,
    )
    qualifying_config = OrderbookMicrostructureStrategyConfig(
        imbalance_weight=Decimal("0.40"),
        maximum_probability_adjustment=Decimal("0.40"),
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
        minimum_conservative_advantage=Decimal("0.01"),
    )
    qualifying = evaluate_retained_microstructure(
        campaign_id="strategy-shadow-demo-v1",
        market=market,
        feature=positive_feature,
        configuration=qualifying_config,
        decision_at=now,
    )
    replay = evaluate_retained_microstructure(
        campaign_id="strategy-shadow-demo-v1",
        market=market,
        feature=positive_feature,
        configuration=qualifying_config,
        decision_at=now,
    )
    scoring = strategy_shadow_demo()
    return {
        "positive_adjustment": positive_feature.depth_imbalance,
        "negative_adjustment": negative_feature.depth_imbalance,
        "no_proposal_prediction_retained": no_proposal.prediction is not None
        and no_proposal.opportunity is None,
        "qualifying_shadow_proposal_id": qualifying.prediction.proposal_id
        if qualifying.prediction
        else None,
        "deterministic_replay": qualifying.prediction
        and replay.prediction
        and qualifying.prediction.prediction_id == replay.prediction.prediction_id,
        "stale_reasons": stale_feature.reason_codes,
        "one_sided_reasons": one_sided_feature.reason_codes,
        "market_consensus_comparison": scoring["market_baseline_brier_score"],
        "resolved_outcome_scoring": scoring["brier_score"],
        "promotion_readiness": "FORWARD_EVIDENCE_REQUIRED_PAPER_DISABLED",
        "financial_counts": scoring["financial_counts_after"],
        "provider_order_calls": 0,
    }


def market_dynamics_shadow_demo() -> dict[str, object]:
    """Exercise exact temporal price evidence without provider or financial writes."""

    from apex.application.market_dynamics_shadow import evaluate_retained_market_dynamics
    from apex.application.strategy_shadow import RetainedMarketEvidence
    from apex.domain.market_dynamics import (
        MarketDynamicsMarketEvidence,
        MarketDynamicsObservation,
        MarketDynamicsStrategyConfig,
        evaluate_market_dynamics_features,
    )
    from apex.domain.public_market import PublicMarketProvider

    now = datetime.now(tz=UTC).replace(microsecond=0)
    rule_hash = ContentHash("b" * 64)

    def evidence(
        values: tuple[tuple[str, str], ...], *, seconds: tuple[int, ...] = (20, 10, 0)
    ) -> MarketDynamicsMarketEvidence:
        observations = tuple(
            MarketDynamicsObservation(
                snapshot_id=f"market-dynamics-demo-snapshot-{ordinal}-{bid}-{ask}",
                provider=PublicMarketProvider.KALSHI,
                provider_market_ticker="KX-MARKET-DYNAMICS-DEMO",
                provider_event_ticker="KX-MARKET-DYNAMICS-DEMO-EVENT",
                series_ticker="KX-MARKET-DYNAMICS-DEMO-SERIES",
                observed_at=now - timedelta(seconds=seconds[ordinal]),
                available_at=now - timedelta(seconds=seconds[ordinal] - 1),
                close_at=now + timedelta(hours=1),
                status="open",
                rule_content_hash=rule_hash,
                yes_bid=Decimal(bid),
                yes_ask=Decimal(ask),
            )
            for ordinal, (bid, ask) in enumerate(values)
        )
        return MarketDynamicsMarketEvidence(
            market_snapshot_id=observations[-1].snapshot_id,
            provider=PublicMarketProvider.KALSHI,
            provider_market_ticker="KX-MARKET-DYNAMICS-DEMO",
            provider_event_ticker="KX-MARKET-DYNAMICS-DEMO-EVENT",
            series_ticker="KX-MARKET-DYNAMICS-DEMO-SERIES",
            active=True,
            observations=observations,
        )

    default = MarketDynamicsStrategyConfig()
    upward_evidence = evidence((("0.39", "0.41"), ("0.44", "0.46"), ("0.49", "0.51")))
    downward_evidence = evidence((("0.59", "0.61"), ("0.54", "0.56"), ("0.49", "0.51")))
    upward = evaluate_market_dynamics_features(
        market=upward_evidence, configuration=default, decision_at=now
    )
    downward = evaluate_market_dynamics_features(
        market=downward_evidence, configuration=default, decision_at=now
    )
    zero = evaluate_market_dynamics_features(
        market=evidence((("0.49", "0.51"), ("0.49", "0.51"), ("0.49", "0.51"))),
        configuration=default,
        decision_at=now,
    )
    excessive_gap = evaluate_market_dynamics_features(
        market=evidence(
            (("0.39", "0.41"), ("0.44", "0.46"), ("0.49", "0.51")),
            seconds=(700, 10, 0),
        ),
        configuration=default,
        decision_at=now,
    )
    stale = evaluate_market_dynamics_features(
        market=upward_evidence,
        configuration=MarketDynamicsStrategyConfig(maximum_observation_age=timedelta(seconds=5)),
        decision_at=now + timedelta(seconds=10),
    )
    wide = evaluate_market_dynamics_features(
        market=evidence((("0.39", "0.41"), ("0.44", "0.46"), ("0.30", "0.60"))),
        configuration=default,
        decision_at=now,
    )
    market = RetainedMarketEvidence(
        snapshot_id=upward_evidence.market_snapshot_id,
        provider="KALSHI",
        provider_market_ticker="KX-MARKET-DYNAMICS-DEMO",
        provider_event_ticker="KX-MARKET-DYNAMICS-DEMO-EVENT",
        observed_at=now,
        available_at=now,
        close_at=now + timedelta(hours=1),
        rule_text="Synthetic retained binary market dynamics rule.",
        rule_content_hash=rule_hash,
        yes_bid=Decimal("0.49"),
        yes_ask=Decimal("0.51"),
        no_ask=Decimal("0.51"),
        visible_yes_quantity=Decimal("10"),
        fee_model_version="kalshi-quadratic-taker-v1",
        expected_fee=Decimal(0),
    )
    no_proposal = evaluate_retained_market_dynamics(
        campaign_id="market-dynamics-demo-v1",
        market=market,
        feature=upward,
        configuration=default,
        decision_at=now,
    )
    qualifying_config = MarketDynamicsStrategyConfig(
        window_momentum_weight=Decimal("0.40"),
        recent_momentum_weight=Decimal(0),
        directional_persistence_weight=Decimal(0),
        maximum_probability_adjustment=Decimal("0.40"),
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
    )
    qualifying = evaluate_retained_market_dynamics(
        campaign_id="market-dynamics-demo-v1",
        market=market,
        feature=upward,
        configuration=qualifying_config,
        decision_at=now,
    )
    replay = evaluate_retained_market_dynamics(
        campaign_id="market-dynamics-demo-v1",
        market=market,
        feature=upward,
        configuration=qualifying_config,
        decision_at=now,
    )
    scoring = strategy_shadow_demo()
    return {
        "upward_window_momentum": upward.window_momentum,
        "downward_window_momentum": downward.window_momentum,
        "zero_movement_reasons": zero.reason_codes,
        "excessive_gap_reasons": excessive_gap.reason_codes,
        "stale_reasons": stale.reason_codes,
        "widening_spread_reasons": wide.reason_codes,
        "valid_prediction_without_proposal": no_proposal.prediction is not None
        and no_proposal.opportunity is None,
        "qualifying_shadow_proposal_id": qualifying.prediction.proposal_id
        if qualifying.prediction
        else None,
        "deterministic_replay": qualifying.prediction is not None
        and replay.prediction is not None
        and qualifying.prediction.prediction_id == replay.prediction.prediction_id,
        "market_consensus_comparison": scoring["market_baseline_brier_score"],
        "microstructure_dynamics_comparison": {
            "market_dynamics_conservative_yes": qualifying.probability.conservative_yes_probability
            if qualifying.probability
            else None,
            "market_consensus_yes": Decimal("0.50"),
            "ensembling": False,
        },
        "resolved_outcome_scoring": scoring["brier_score"],
        "promotion_readiness": "FORWARD_EVIDENCE_REQUIRED_PAPER_DISABLED",
        "financial_counts": scoring["financial_counts_after"],
        "provider_order_calls": 0,
    }


def _run_operating_cycle_command(arguments: argparse.Namespace) -> object:
    from apex.adapters.operating_cycle_postgres import (
        PostgresOperatingCycleRepository,
        PostgresOperatingStateSource,
    )
    from apex.adapters.operating_cycle_tasks import (
        ExistingCapabilityOperatingTaskExecutor,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.operating_cycle import (
        ExecuteOperatingCycle,
        PlanOperatingDueWork,
        RegisterOperatingCyclePlan,
        SetOperatingCycleControl,
        SetPaperActivation,
    )
    from apex.domain.operating_cycle import OperatingControlState, PaperActivationState

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    authority = {
        "network_behavior": "DUE_TASKS_ONLY_EXACT_BOUNDED_PUBLIC_GET",
        "task_effects": "RESEARCH_EVIDENCE_ONLY",
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "reservation_authority": "NONE",
        "order_authority": "NONE",
        "broker_authority": "NONE",
        "financial_mutation_capability": "NONE",
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresOperatingCycleRepository(engine)
    source = PostgresOperatingStateSource(engine, repository)
    try:
        if arguments.command == "operating-plan-register":
            plan, replay = RegisterOperatingCyclePlan(repository).execute(
                plan_id=arguments.plan_id,
                crypto_pulse_plan_id=arguments.crypto_pulse_plan_id,
                actor=arguments.actor,
                created_at=parsed(arguments.created_at),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "plan": plan,
                "created_evidence": () if replay else (plan.plan_id,),
                "reused_evidence": (plan.plan_id,) if replay else (),
                **authority,
            }
        if arguments.command == "operating-plan-inspect":
            retained_plan = repository.plan(arguments.plan_id)
            if retained_plan is None:
                raise ValueError("OPERATING_PLAN_NOT_FOUND")
            controls = repository.control_events(arguments.plan_id)
            paper_activations = repository.paper_activation_events(arguments.plan_id)
            paper_enabled = bool(paper_activations) and (
                paper_activations[-1].state is PaperActivationState.ENABLED
            )
            return {
                "plan": retained_plan,
                "operator_control": controls[-1] if controls else None,
                "paper_activation": paper_activations[-1] if paper_activations else None,
                "paper_activation_history": paper_activations,
                "created_evidence": (),
                "reused_evidence": (retained_plan.plan_id,),
                **authority,
                "paper_eligible": paper_enabled,
            }
        if arguments.command == "operating-paper-activation-inspect":
            paper_activations = repository.paper_activation_events(arguments.plan_id)
            paper_enabled = bool(paper_activations) and (
                paper_activations[-1].state is PaperActivationState.ENABLED
            )
            return {
                "plan_id": arguments.plan_id,
                "paper_activation": paper_activations[-1] if paper_activations else None,
                "paper_activation_history": paper_activations,
                "created_evidence": (),
                "reused_evidence": (arguments.plan_id,),
                **authority,
                "paper_eligible": paper_enabled,
            }
        if arguments.command == "operating-due-plan":
            due = PlanOperatingDueWork(repository, source).execute(
                cycle_id=arguments.cycle_id,
                plan_id=arguments.plan_id,
                as_of=parsed(arguments.as_of),
            )
            return {
                "due_work": due,
                "created_evidence": (),
                "reused_evidence": due.tasks[0].source_evidence_ids,
                **authority,
            }
        if arguments.command in (
            "operating-cycle-execute",
            "operating-cycle-replay",
        ):
            result = ExecuteOperatingCycle(
                repository,
                source,
                ExistingCapabilityOperatingTaskExecutor(engine, source),
            ).execute(
                cycle_id=arguments.cycle_id,
                plan_id=arguments.plan_id,
                as_of=parsed(arguments.as_of),
                executed_at=parsed(arguments.executed_at),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "result": result,
                "created_evidence": ()
                if result.idempotent_replay
                else (
                    result.cycle.cycle_id,
                    *(item.task_result_id for item in result.tasks),
                ),
                "reused_evidence": tuple(
                    value for item in result.tasks for value in item.reused_identities
                ),
                **authority,
            }
        if arguments.command == "operating-cycle-inspect":
            cycle = repository.cycle(arguments.cycle_id)
            if cycle is None:
                raise ValueError("OPERATING_CYCLE_NOT_FOUND")
            return {
                "cycle": cycle,
                "events": repository.cycle_events(arguments.cycle_id),
                "due_work": repository.due_work(arguments.cycle_id),
                "tasks": repository.task_results(arguments.cycle_id),
                "created_evidence": (),
                "reused_evidence": (cycle.cycle_id,),
                **authority,
            }
        if arguments.command == "operating-task-inspect":
            task_result = repository.task_result(arguments.task_result_id)
            if task_result is None:
                raise ValueError("OPERATING_TASK_RESULT_NOT_FOUND")
            return {
                "task_result": task_result,
                "created_evidence": (),
                "reused_evidence": (task_result.task_result_id,),
                **authority,
            }
        if arguments.command in ("operating-cycle-pause", "operating-cycle-resume"):
            event, replay = SetOperatingCycleControl(repository).execute(
                event_id=arguments.event_id,
                plan_id=arguments.plan_id,
                state=(
                    OperatingControlState.PAUSED
                    if arguments.command == "operating-cycle-pause"
                    else OperatingControlState.ENABLED
                ),
                reason=arguments.reason,
                actor=arguments.actor,
                occurred_at=parsed(arguments.occurred_at),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "control_event": event,
                "created_evidence": () if replay else (event.event_id,),
                "reused_evidence": (event.event_id,) if replay else (),
                **authority,
            }
        if arguments.command in ("operating-paper-activate", "operating-paper-deactivate"):
            paper_event, paper_replay = SetPaperActivation(repository).execute(
                event_id=arguments.event_id,
                plan_id=arguments.plan_id,
                state=(
                    PaperActivationState.ENABLED
                    if arguments.command == "operating-paper-activate"
                    else PaperActivationState.DISABLED
                ),
                reason=arguments.reason,
                actor=arguments.actor,
                occurred_at=parsed(arguments.occurred_at),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "paper_activation_event": paper_event,
                "created_evidence": () if paper_replay else (paper_event.event_id,),
                "reused_evidence": (paper_event.event_id,) if paper_replay else (),
                **authority,
                "paper_eligible": paper_event.state is PaperActivationState.ENABLED,
            }
        if arguments.command == "operating-brief-inspect":
            brief = repository.brief(arguments.brief_id)
            if brief is None:
                raise ValueError("OPERATING_BRIEF_NOT_FOUND")
            return {
                "operating_brief": brief,
                "created_evidence": (),
                "reused_evidence": (brief.brief_id,),
                **authority,
            }
        if arguments.command == "operating-health-inspect":
            health = repository.health(arguments.health_id)
            if health is None:
                raise ValueError("OPERATING_HEALTH_NOT_FOUND")
            return {
                "operating_health": health,
                "created_evidence": (),
                "reused_evidence": (health.health_id,),
                **authority,
            }
        if arguments.command == "operating-daily-checkpoint-inspect":
            return _build_operating_daily_checkpoint(
                engine=engine,
                repository=repository,
                plan_id=arguments.plan_id,
                cycle_id=arguments.cycle_id,
                runner_id=arguments.runner_id,
            )
        if arguments.command == "operating-cycle-worker":
            return _run_supervised_operating_cycle_worker(
                engine=engine,
                repository=repository,
                source=source,
                plan_id=arguments.plan_id,
                runner_id=arguments.runner_id,
                run_instance_id=arguments.run_instance_id,
                once=arguments.once,
                poll_seconds=arguments.poll_seconds,
                max_cycles=arguments.max_cycles,
                authority=authority,
            )
        if arguments.command == "live-observer-worker":
            return _run_supervised_live_observer_worker(
                engine=engine,
                plan_id=arguments.plan_id,
                runner_id=arguments.runner_id,
                run_instance_id=arguments.run_instance_id,
                once=arguments.once,
                poll_seconds=arguments.poll_seconds,
                max_cycles=arguments.max_cycles,
                authority=authority,
            )
        raise ValueError("OPERATING_CYCLE_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_cross_market_research_command(arguments: argparse.Namespace) -> object:
    """Cross-Market Feature, Regime, and Controlled Hypothesis Research v1
    (section 23): bounded research-agenda/hypothesis-proposal/agenda-run/
    knowledge/contradiction/follow-up/quality-report commands, wired
    entirely through existing application services and repositories.
    Every retained record here carries ``authority = "NONE"``; nothing in
    this command family ever creates a Strategy, Shared Opportunity,
    PAPER order, or LIVE authority."""

    from apex.adapters.cross_market_research_postgres import (
        PostgresCrossMarketResearchRepository,
    )
    from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
    from apex.adapters.genuine_multi_asset_history_postgres import (
        PostgresGenuineMultiAssetHistoryRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.cross_market_research import (
        AdvanceAgendaRun,
        BuildCrossMarketPanel,
        CrossMarketResearchNotFound,
        RegisterAgendaRun,
        build_agenda_timeline,
        build_v1_agenda,
        generate_v1_agenda_hypotheses,
    )

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    authority = {
        "financial_mutation_capability": "NONE",
        "strategy_authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCrossMarketResearchRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    try:
        if arguments.command == "research-agenda-create":
            draft_agenda = build_v1_agenda(
                dataset_snapshot_id=arguments.dataset_snapshot_id,
                replay_run_id=arguments.replay_run_id,
                cross_market_dataset_id=arguments.cross_market_dataset_id,
                cohort_policy_id=arguments.cohort_policy_id,
                registered_at=parsed(arguments.created_at),
            )
            existing = repository.agenda_by_id(draft_agenda.agenda_id)
            saved = repository.save_agenda(draft_agenda)
            return {
                "agenda": saved,
                "created_evidence": () if existing else (saved.agenda_id,),
                "reused_evidence": (saved.agenda_id,) if existing else (),
                **authority,
            }
        if arguments.command == "research-agenda-inspect":
            inspect_agenda = repository.agenda_by_id(arguments.agenda_id)
            if inspect_agenda is None:
                raise ValueError("RESEARCH_AGENDA_NOT_FOUND")
            hypotheses = repository.hypotheses_for_agenda(arguments.agenda_id)
            run = repository.agenda_run_by_agenda(arguments.agenda_id)
            quality_report = repository.quality_report_by_agenda(arguments.agenda_id)
            return {
                "agenda": inspect_agenda,
                "hypothesis_count": len(hypotheses),
                "agenda_run": run,
                "quality_report": quality_report,
                **authority,
            }
        if arguments.command == "hypothesis-proposals-generate":
            proposal_agenda = repository.agenda_by_id(arguments.agenda_id)
            if proposal_agenda is None:
                raise ValueError("RESEARCH_AGENDA_NOT_FOUND")
            hypotheses = generate_v1_agenda_hypotheses(
                agenda=proposal_agenda, created_at=parsed(arguments.created_at)
            )
            created: list[str] = []
            reused: list[str] = []
            for hypothesis in hypotheses:
                existing_hypothesis = repository.hypothesis_by_id(hypothesis.hypothesis_id)
                repository.save_hypothesis(hypothesis)
                (reused if existing_hypothesis else created).append(hypothesis.hypothesis_id)
            return {
                "agenda_id": proposal_agenda.agenda_id,
                "hypotheses": hypotheses,
                "created_evidence": tuple(created),
                "reused_evidence": tuple(reused),
                **authority,
            }
        if arguments.command == "hypothesis-proposals-list":
            hypotheses = repository.hypotheses_for_agenda(arguments.agenda_id)
            families: dict[str, int] = {}
            for item in hypotheses:
                families[item.family.value] = families.get(item.family.value, 0) + 1
            return {
                "agenda_id": arguments.agenda_id,
                "hypotheses": hypotheses,
                "families": tuple(sorted(families.items())),
                "negative_control_count": sum(1 for item in hypotheses if item.is_negative_control),
                **authority,
            }
        if arguments.command in ("agenda-run-start", "agenda-run-resume"):
            run_agenda = repository.agenda_by_id(arguments.agenda_id)
            if run_agenda is None:
                raise ValueError("RESEARCH_AGENDA_NOT_FOUND")
            hypotheses = repository.hypotheses_for_agenda(arguments.agenda_id)
            if not hypotheses:
                raise ValueError("NO_HYPOTHESES_GENERATED_FOR_AGENDA_YET")
            historical_repository = PostgresHistoricalMarketDataRepository(engine)
            panel = BuildCrossMarketPanel(
                historical_repository=historical_repository,
                evidence=PostgresSecuritiesRepository(engine),
                genuine_repository=PostgresGenuineMultiAssetHistoryRepository(engine),
                crypto_repository=PostgresCryptoResearchRepository(engine),
            ).execute(
                equity_snapshot_id=run_agenda.dataset_snapshot_id,
                cross_market_dataset_id=run_agenda.cross_market_dataset_id,
                horizons=run_agenda.allowed_horizons_sessions,
            )
            timeline = build_agenda_timeline(
                panel=panel,
                agenda=run_agenda,
                train_end=arguments.train_end,
                validation_end=arguments.validation_end,
                created_at=parsed(arguments.created_at),
            )
            created_at = parsed(arguments.created_at)
            if arguments.command == "agenda-run-start":
                RegisterAgendaRun(repository=repository).execute(
                    agenda=run_agenda, hypotheses=hypotheses, created_at=created_at
                )
            run = AdvanceAgendaRun(
                repository=repository,
                methodology_repository=methodology_repository,
                timeline=timeline,
                panel_warnings=panel.warnings,
            ).execute(
                agenda=run_agenda,
                panel=panel,
                maximum_hypotheses=arguments.maximum_hypotheses,
                created_at=created_at,
            )
            return {"agenda_run": run, "panel_warnings": panel.warnings, **authority}
        if arguments.command == "agenda-run-inspect":
            run = repository.agenda_run_by_agenda(arguments.agenda_id)
            if run is None:
                raise ValueError("AGENDA_RUN_NOT_FOUND")
            executions = repository.executions_for_agenda(arguments.agenda_id)
            return {
                "agenda_run": run,
                "executions_completed": len(executions),
                **authority,
            }
        if arguments.command == "research-knowledge-inspect":
            history = repository.knowledge_entries_for_hypothesis(arguments.hypothesis_id)
            if not history:
                raise ValueError("RESEARCH_KNOWLEDGE_NOT_FOUND")
            execution = repository.execution_by_hypothesis(arguments.hypothesis_id)
            return {
                "hypothesis_id": arguments.hypothesis_id,
                "knowledge_history": history,
                "latest": history[-1],
                "execution": execution,
                **authority,
            }
        if arguments.command == "research-contradictions-list":
            return {"contradictions": repository.list_contradictions(), **authority}
        if arguments.command == "follow-up-proposals-list":
            return {"follow_up_proposals": repository.list_follow_ups(), **authority}
        if arguments.command == "research-agenda-quality-inspect":
            report = repository.quality_report_by_agenda(arguments.agenda_id)
            if report is None:
                raise ValueError("RESEARCH_AGENDA_QUALITY_REPORT_NOT_FOUND")
            return {"quality_report": report, **authority}
        raise ValueError("CROSS_MARKET_RESEARCH_COMMAND_UNSUPPORTED")
    except CrossMarketResearchNotFound as error:
        raise ValueError(str(error)) from error
    finally:
        engine.dispose()


def _run_research_calibration_command(arguments: argparse.Namespace) -> object:
    """Research Signal Calibration and Negative-Control Hardening v1:
    bounded calibration-plan/null-control/run/report/recalibration/
    follow-up-priority commands, wired entirely through existing and new
    application services and repositories. Every retained record here
    carries ``authority = "NONE"``; nothing in this command family ever
    creates a Strategy, Shared Opportunity, PAPER order, or LIVE
    authority, and nothing here claims trading edge or alpha."""

    from apex.adapters.cross_market_research_postgres import (
        PostgresCrossMarketResearchRepository,
    )
    from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
    from apex.adapters.genuine_multi_asset_history_postgres import (
        PostgresGenuineMultiAssetHistoryRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_calibration_postgres import PostgresCalibrationRepository
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.cross_market_research import BuildCrossMarketPanel, build_agenda_timeline
    from apex.application.research_calibration import (
        AdvanceCalibrationRun,
        AssessFollowUpPriority,
        CalibrationNotFound,
        RecalibratePriorAgenda,
        RegisterCalibrationRun,
        build_v1_calibration_plan,
        generate_v1_null_control_specs,
    )

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    authority = {
        "financial_mutation_capability": "NONE",
        "strategy_authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCalibrationRepository(engine)
    cross_market_repository = PostgresCrossMarketResearchRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    try:
        if arguments.command == "research-calibration-create":
            source_agenda = cross_market_repository.agenda_by_id(arguments.source_agenda_id)
            if source_agenda is None:
                raise ValueError("SOURCE_RESEARCH_AGENDA_NOT_FOUND")
            draft_plan = build_v1_calibration_plan(
                dataset_snapshot_id=arguments.dataset_snapshot_id,
                replay_run_id=arguments.replay_run_id,
                cross_market_dataset_id=arguments.cross_market_dataset_id,
                source_agenda_id=arguments.source_agenda_id,
                registered_at=parsed(arguments.created_at),
            )
            existing_plan = repository.plan_by_id(draft_plan.calibration_id)
            saved_plan = repository.save_plan(draft_plan)
            specs = generate_v1_null_control_specs(
                calibration=saved_plan, created_at=parsed(arguments.created_at)
            )
            created: list[str] = []
            reused: list[str] = []
            for spec in specs:
                existing_spec = repository.spec_by_id(spec.spec_id)
                repository.save_spec(spec)
                (reused if existing_spec else created).append(spec.spec_id)
            return {
                "calibration": saved_plan,
                "null_control_count": len(specs),
                "created_evidence": () if existing_plan else tuple(created),
                "reused_evidence": tuple(reused) if existing_plan else (),
                **authority,
            }
        if arguments.command == "research-calibration-inspect":
            plan = repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_PLAN_NOT_FOUND")
            specs = repository.specs_for_calibration(arguments.calibration_id)
            families: dict[str, int] = {}
            for spec_item in specs:
                families[spec_item.family.value] = families.get(spec_item.family.value, 0) + 1
            run = repository.run_by_calibration(arguments.calibration_id)
            report = repository.report_by_calibration(arguments.calibration_id)
            return {
                "calibration": plan,
                "null_control_count": len(specs),
                "families": tuple(sorted(families.items())),
                "calibration_run": run,
                "report": report,
                **authority,
            }
        if arguments.command in (
            "research-calibration-run-start",
            "research-calibration-run-resume",
        ):
            plan = repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_PLAN_NOT_FOUND")
            source_agenda = cross_market_repository.agenda_by_id(plan.source_agenda_id)
            if source_agenda is None:
                raise ValueError("SOURCE_RESEARCH_AGENDA_NOT_FOUND")
            specs = repository.specs_for_calibration(arguments.calibration_id)
            if not specs:
                raise ValueError("NO_NULL_CONTROLS_GENERATED_FOR_CALIBRATION_YET")
            historical_repository = PostgresHistoricalMarketDataRepository(engine)
            panel = BuildCrossMarketPanel(
                historical_repository=historical_repository,
                evidence=PostgresSecuritiesRepository(engine),
                genuine_repository=PostgresGenuineMultiAssetHistoryRepository(engine),
                crypto_repository=PostgresCryptoResearchRepository(engine),
            ).execute(
                equity_snapshot_id=source_agenda.dataset_snapshot_id,
                cross_market_dataset_id=source_agenda.cross_market_dataset_id,
                horizons=source_agenda.allowed_horizons_sessions,
            )
            timeline = build_agenda_timeline(
                panel=panel,
                agenda=source_agenda,
                train_end=arguments.train_end,
                validation_end=arguments.validation_end,
                created_at=parsed(arguments.created_at),
            )
            created_at = parsed(arguments.created_at)
            if arguments.command == "research-calibration-run-start":
                RegisterCalibrationRun(repository=repository).execute(
                    calibration=plan, specs=specs, created_at=created_at
                )
            run = AdvanceCalibrationRun(
                repository=repository,
                methodology_repository=methodology_repository,
                timeline=timeline,
            ).execute(
                calibration=plan,
                panel=panel,
                maximum_specs=arguments.maximum_specs,
                created_at=created_at,
            )
            return {"calibration_run": run, "panel_warnings": panel.warnings, **authority}
        if arguments.command == "research-calibration-run-inspect":
            run = repository.run_by_calibration(arguments.calibration_id)
            if run is None:
                raise ValueError("CALIBRATION_RUN_NOT_FOUND")
            outcomes = repository.outcomes_for_run(run.calibration_run_id)
            return {"calibration_run": run, "outcomes_completed": len(outcomes), **authority}
        if arguments.command == "research-calibration-report-inspect":
            report = repository.report_by_calibration(arguments.calibration_id)
            if report is None:
                raise ValueError("CALIBRATION_REPORT_NOT_FOUND")
            breakdowns = repository.breakdowns_for_calibration(arguments.calibration_id)
            comparisons = repository.method_comparisons_for_calibration(arguments.calibration_id)
            return {
                "report": report,
                "breakdowns": breakdowns,
                "method_comparisons": comparisons,
                **authority,
            }
        if arguments.command == "research-evidence-grade-inspect":
            recalibrations = repository.recalibrations_for_calibration(arguments.calibration_id)
            match = next(
                (item for item in recalibrations if item.hypothesis_id == arguments.hypothesis_id),
                None,
            )
            if match is None:
                raise ValueError("EVIDENCE_GRADE_RECALIBRATION_NOT_FOUND")
            return {"recalibration": match, **authority}
        if arguments.command == "research-agenda-recalibrate":
            plan = repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_PLAN_NOT_FOUND")
            run = repository.run_by_calibration(arguments.calibration_id)
            if run is None or run.status.value != "COMPLETE":
                raise ValueError("CALIBRATION_RUN_NOT_COMPLETE")
            null_cohens_d = []
            for outcome in repository.outcomes_for_run(run.calibration_run_id):
                if outcome.validation_trial_id:
                    trial = methodology_repository.trial_by_id(outcome.validation_trial_id)
                    if (
                        trial is not None
                        and trial.diagnostics.effect_size is not None
                        and trial.diagnostics.effect_size.cohens_d is not None
                    ):
                        null_cohens_d.append(trial.diagnostics.effect_size.cohens_d)
            created_at = parsed(arguments.created_at)
            recalibrations = RecalibratePriorAgenda(
                cross_market_repository=cross_market_repository,
                methodology_repository=methodology_repository,
            ).execute(
                calibration=plan,
                source_agenda_id=plan.source_agenda_id,
                null_cohens_d=tuple(null_cohens_d),
                created_at=created_at,
            )
            for recalibration_item in recalibrations:
                repository.save_recalibration(recalibration_item)
            assessments = AssessFollowUpPriority(
                cross_market_repository=cross_market_repository
            ).execute(calibration=plan, recalibrations=recalibrations, created_at=created_at)
            for assessment_item in assessments:
                repository.save_follow_up_priority(assessment_item)
            return {
                "recalibrations": recalibrations,
                "follow_up_priorities": assessments,
                "null_control_sample": len(null_cohens_d),
                **authority,
            }
        if arguments.command == "follow-up-priority-list":
            return {
                "follow_up_priorities": repository.follow_up_priorities_for_calibration(
                    arguments.calibration_id
                ),
                **authority,
            }
        raise ValueError("RESEARCH_CALIBRATION_COMMAND_UNSUPPORTED")
    except CalibrationNotFound as error:
        raise ValueError(str(error)) from error
    finally:
        engine.dispose()


def _v2_target_hypothesis_ids(
    *,
    calibration_repository: Any,
    source_calibration_id: str,
) -> set[str]:
    """Dynamically discovers which already-retained genuine hypotheses
    sections 17/18/19 want reassessed under v2 -- never hardcoded ids:
    every hypothesis v1's own recalibration marked ``WEAK`` (covers the
    two genuine ``WEAK`` findings and the negative control) union every
    parent hypothesis of a v1 ``JUSTIFIED`` follow-up (covers the three
    currently JUSTIFIED proposals' parents)."""

    from apex.domain.research_calibration import FollowUpPriorityClass

    weak = {
        item.hypothesis_id
        for item in calibration_repository.recalibrations_for_calibration(source_calibration_id)
        if item.original_knowledge_status == "WEAK"
    }
    justified_parents = {
        item.hypothesis_id
        for item in calibration_repository.recalibrations_for_calibration(source_calibration_id)
        if item.follow_up_priority is FollowUpPriorityClass.JUSTIFIED
    }
    return weak | justified_parents


def _v2_priority_strata(
    *,
    calibration_repository: Any,
    cross_market_repository: Any,
    source_calibration_id: str,
    source_agenda_id: str,
) -> frozenset[str]:
    """Section 12's predeclared walk-forward-null priority strata: the
    matched-stratum keys of every hypothesis v1's own recalibration
    (``PriorHypothesisRecalibration.follow_up_priority``) independently
    rated ``JUSTIFIED`` -- computed from those hypotheses' own declared
    template/market-scope/horizon fields, never from an observed v2
    outcome. This necessarily includes the three follow-up-linked
    hypotheses (``AssessFollowUpPriority`` copies its parent hypothesis's
    ``follow_up_priority`` verbatim onto every follow-up proposal it
    classifies -- see ``application.research_calibration.
    AssessFollowUpPriority``), plus any further hypothesis v1 rated
    JUSTIFIED without ever generating a retained follow-up proposal for
    it -- a deliberately inclusive policy for "the highest-value
    walk-forward question," not a narrower one that would silently skip
    JUSTIFIED evidence just because no follow-up proposal happens to
    reference it."""

    from apex.application.research_calibration_v2 import conditioning_shape_for_hypothesis
    from apex.domain.cross_market_research import OutcomeFamily
    from apex.domain.research_calibration import FollowUpPriorityClass
    from apex.domain.research_calibration_v2 import (
        matched_stratum_key,
        research_design_for_hypothesis,
    )

    justified_ids = {
        item.hypothesis_id
        for item in calibration_repository.recalibrations_for_calibration(source_calibration_id)
        if item.follow_up_priority is FollowUpPriorityClass.JUSTIFIED
    }
    if not justified_ids:
        return frozenset()
    hypotheses = cross_market_repository.hypotheses_for_agenda(source_agenda_id)
    strata: set[str] = set()
    for hypothesis in hypotheses:
        if hypothesis.hypothesis_id not in justified_ids:
            continue
        design = research_design_for_hypothesis(
            template_id=hypothesis.template_id, market_scope=hypothesis.market_scope
        )
        shape = conditioning_shape_for_hypothesis(hypothesis, design)
        outcome_family = OutcomeFamily(_v2_outcome_family_value(hypothesis.outcome_id.value))
        strata.add(
            matched_stratum_key(
                research_design=design,
                outcome_family=outcome_family,
                horizon_sessions=hypothesis.horizon_sessions,
                market_scope=hypothesis.market_scope,
                conditioning_shape=shape,
            )
        )
    return frozenset(strata)


def _v2_outcome_family_value(outcome_id_value: str) -> str:
    from apex.application.research_calibration import _OUTCOME_FAMILY_BY_OUTCOME_ID

    family = _OUTCOME_FAMILY_BY_OUTCOME_ID.get(outcome_id_value)
    if family is None:
        raise ValueError(f"UNKNOWN_OUTCOME_FAMILY_FOR_OUTCOME_ID:{outcome_id_value}")
    return family.value


def _run_research_calibration_v2_command(arguments: argparse.Namespace) -> object:
    """Research Calibration v2: Template-Matched Nulls and Stability --
    bounded calibration-v2 create/inspect/run/resume/inspect,
    calibration-strata/calibration-stability inspect, follow-up-requalify,
    and evidence-grade-v2 inspect commands. Every retained record here
    carries ``authority = "NONE"``; nothing in this command family ever
    creates a Strategy, Shared Opportunity, PAPER order, or LIVE
    authority, and the three currently JUSTIFIED follow-up hypotheses are
    never executed by any command here -- only requalified."""

    from apex.adapters.cross_market_research_postgres import (
        PostgresCrossMarketResearchRepository,
    )
    from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
    from apex.adapters.genuine_multi_asset_history_postgres import (
        PostgresGenuineMultiAssetHistoryRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_calibration_postgres import PostgresCalibrationRepository
    from apex.adapters.research_calibration_v2_postgres import PostgresCalibrationV2Repository
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.cross_market_research import BuildCrossMarketPanel, build_agenda_timeline
    from apex.application.research_calibration import CalibrationNotFound
    from apex.application.research_calibration_v2 import (
        AdvanceCalibrationRunV2,
        FinalizeCalibrationV2,
        ReassessGenuineFindingsV2,
        RegisterCalibrationRunV2,
        RequalifyFollowUpsV2,
        build_matched_pools,
        build_v2_calibration_plan,
        collect_v1_matched_pool,
        generate_v2_null_control_specs,
        select_walk_forward_null_subset,
    )
    from apex.domain.cross_market_research import OutcomeFamily
    from apex.domain.research_calibration_v2 import (
        ConditioningShape,
        ResearchDesign,
        research_design_for_hypothesis,
    )
    from apex.domain.research_calibration_v2 import matched_stratum_key as _matched_stratum_key

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    authority = {
        "financial_mutation_capability": "NONE",
        "strategy_authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    calibration_repository = PostgresCalibrationRepository(engine)
    repository = PostgresCalibrationV2Repository(engine)
    cross_market_repository = PostgresCrossMarketResearchRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    try:
        if arguments.command == "calibration-v2-create":
            source_plan = calibration_repository.plan_by_id(arguments.source_calibration_id)
            if source_plan is None:
                raise ValueError("SOURCE_V1_CALIBRATION_PLAN_NOT_FOUND")
            created_at = parsed(arguments.created_at)
            draft_plan = build_v2_calibration_plan(
                dataset_snapshot_id=source_plan.dataset_snapshot_id,
                replay_run_id=source_plan.replay_run_id,
                cross_market_dataset_id=source_plan.cross_market_dataset_id,
                source_agenda_id=source_plan.source_agenda_id,
                source_calibration_id=source_plan.calibration_id,
                registered_at=created_at,
            )
            existing_plan = calibration_repository.plan_by_id(draft_plan.calibration_id)
            saved_plan = calibration_repository.save_plan(draft_plan)
            specs = generate_v2_null_control_specs(calibration=saved_plan, created_at=created_at)
            created: list[str] = []
            reused: list[str] = []
            for spec in specs:
                existing_spec = repository.spec_by_id(spec.spec_id)
                repository.save_spec(spec)
                (reused if existing_spec else created).append(spec.spec_id)
            return {
                "calibration": saved_plan,
                "source_calibration_id": source_plan.calibration_id,
                "null_control_count": len(specs),
                "created_evidence": () if existing_plan else tuple(created),
                "reused_evidence": tuple(reused) if existing_plan else (),
                **authority,
            }
        if arguments.command == "calibration-v2-inspect":
            plan = calibration_repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_V2_PLAN_NOT_FOUND")
            specs = repository.specs_for_calibration(arguments.calibration_id)
            designs: dict[str, int] = {}
            for spec_item in specs:
                designs[spec_item.research_design.value] = (
                    designs.get(spec_item.research_design.value, 0) + 1
                )
            run = calibration_repository.run_by_calibration(arguments.calibration_id)
            report = calibration_repository.report_by_calibration(arguments.calibration_id)
            return {
                "calibration": plan,
                "null_control_count": len(specs),
                "designs": tuple(sorted(designs.items())),
                "calibration_run": run,
                "report": report,
                **authority,
            }
        if arguments.command in ("calibration-v2-run-start", "calibration-v2-run-resume"):
            plan = calibration_repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_V2_PLAN_NOT_FOUND")
            source_agenda = cross_market_repository.agenda_by_id(plan.source_agenda_id)
            if source_agenda is None:
                raise ValueError("SOURCE_RESEARCH_AGENDA_NOT_FOUND")
            specs = repository.specs_for_calibration(arguments.calibration_id)
            if not specs:
                raise ValueError("NO_V2_NULL_CONTROLS_GENERATED_FOR_CALIBRATION_YET")
            historical_repository = PostgresHistoricalMarketDataRepository(engine)
            panel = BuildCrossMarketPanel(
                historical_repository=historical_repository,
                evidence=PostgresSecuritiesRepository(engine),
                genuine_repository=PostgresGenuineMultiAssetHistoryRepository(engine),
                crypto_repository=PostgresCryptoResearchRepository(engine),
            ).execute(
                equity_snapshot_id=source_agenda.dataset_snapshot_id,
                cross_market_dataset_id=source_agenda.cross_market_dataset_id,
                horizons=source_agenda.allowed_horizons_sessions,
            )
            timeline = build_agenda_timeline(
                panel=panel,
                agenda=source_agenda,
                train_end=arguments.train_end,
                validation_end=arguments.validation_end,
                created_at=parsed(arguments.created_at),
            )
            created_at = parsed(arguments.created_at)
            priority_strata = _v2_priority_strata(
                calibration_repository=calibration_repository,
                cross_market_repository=cross_market_repository,
                source_calibration_id=arguments.source_calibration_id,
                source_agenda_id=plan.source_agenda_id,
            )
            walk_forward_subset = frozenset(
                select_walk_forward_null_subset(specs=specs, priority_strata=priority_strata)
            )
            if arguments.command == "calibration-v2-run-start":
                RegisterCalibrationRunV2(calibration_repository=calibration_repository).execute(
                    calibration=plan, specs=specs, created_at=created_at
                )
            run = AdvanceCalibrationRunV2(
                calibration_repository=calibration_repository,
                calibration_v2_repository=repository,
                methodology_repository=methodology_repository,
                timeline=timeline,
                walk_forward_spec_ids=walk_forward_subset,
            ).execute(
                calibration=plan,
                panel=panel,
                maximum_specs=arguments.maximum_specs,
                created_at=created_at,
            )
            finalized_report = None
            if run.status.value == "COMPLETE":
                finalized_report = (
                    FinalizeCalibrationV2(
                        calibration_repository=calibration_repository,
                        calibration_v2_repository=repository,
                        methodology_repository=methodology_repository,
                        timeline=timeline,
                    )
                    .execute(calibration=plan, run=run, panel=panel, created_at=created_at)
                    .report
                )
            return {
                "calibration_run": run,
                "panel_warnings": panel.warnings,
                "walk_forward_priority_strata": tuple(sorted(priority_strata)),
                "walk_forward_subset_size": len(walk_forward_subset),
                "report": finalized_report,
                **authority,
            }
        if arguments.command == "calibration-v2-run-inspect":
            run = calibration_repository.run_by_calibration(arguments.calibration_id)
            if run is None:
                raise ValueError("CALIBRATION_V2_RUN_NOT_FOUND")
            outcomes = repository.outcomes_for_run(run.calibration_run_id)
            report = calibration_repository.report_by_calibration(arguments.calibration_id)
            return {
                "calibration_run": run,
                "outcomes_completed": len(outcomes),
                "report": report,
                **authority,
            }
        if arguments.command == "calibration-v2-recalibrate-agenda":
            plan = calibration_repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_V2_PLAN_NOT_FOUND")
            report = calibration_repository.report_by_calibration(arguments.calibration_id)
            if report is None:
                raise ValueError("CALIBRATION_V2_REPORT_NOT_FOUND_RUN_TO_COMPLETION_FIRST")
            created_at = parsed(arguments.created_at)
            null_classifications = repository.classifications_for_calibration(
                arguments.calibration_id
            )
            by_stratum, pooled = build_matched_pools(null_classifications)
            target_ids = _v2_target_hypothesis_ids(
                calibration_repository=calibration_repository,
                source_calibration_id=arguments.source_calibration_id,
            )
            hypotheses = tuple(
                item
                for item in cross_market_repository.hypotheses_for_agenda(plan.source_agenda_id)
                if item.hypothesis_id in target_ids
            )
            # Section 3: PREDICTIVE calibration continues to be served by
            # v1's own already-retained population -- merge it into the
            # matched pool only for PREDICTIVE-design targets, filtered to
            # the same outcome family/horizon, never touching v1 evidence.
            v1_specs = calibration_repository.specs_for_calibration(arguments.source_calibration_id)
            v1_run = calibration_repository.run_by_calibration(arguments.source_calibration_id)
            v1_outcomes = (
                calibration_repository.outcomes_for_run(v1_run.calibration_run_id)
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
                outcome_family = OutcomeFamily(
                    _v2_outcome_family_value(hypothesis.outcome_id.value)
                )
                stratum = _matched_stratum_key(
                    research_design=ResearchDesign.PREDICTIVE,
                    outcome_family=outcome_family,
                    horizon_sessions=hypothesis.horizon_sessions,
                    market_scope=hypothesis.market_scope,
                    conditioning_shape=ConditioningShape.UNCONDITIONAL,
                )
                v1_pool = collect_v1_matched_pool(
                    v1_specs=v1_specs,
                    v1_outcomes=v1_outcomes,
                    methodology_repository=methodology_repository,
                    outcome_family=outcome_family,
                    horizon_sessions=hypothesis.horizon_sessions,
                )
                if v1_pool:
                    merged_pools[stratum] = merged_pools.get(stratum, ()) + v1_pool
            reassessment_results = ReassessGenuineFindingsV2(
                cross_market_repository=cross_market_repository,
                methodology_repository=methodology_repository,
            ).execute(
                calibration=plan,
                hypotheses=hypotheses,
                matched_pools=merged_pools,
                pooled=pooled,
                created_at=created_at,
            )
            for classification, recalibration in reassessment_results:
                repository.save_classification(classification)
                calibration_repository.save_recalibration(recalibration)
            return {
                "hypotheses_reassessed": len(reassessment_results),
                "classifications": tuple(item for item, _r in reassessment_results),
                "recalibrations": tuple(item for _c, item in reassessment_results),
                **authority,
            }
        if arguments.command == "calibration-strata-inspect":
            specs = repository.specs_for_calibration(arguments.calibration_id)
            if arguments.research_design:
                specs = tuple(
                    item
                    for item in specs
                    if item.research_design.value == arguments.research_design
                )
            classification_by_subject = {
                item.subject_id: item
                for item in repository.classifications_for_calibration(arguments.calibration_id)
            }
            counts: dict[str, int] = {}
            for spec_item in specs:
                counts[spec_item.matched_stratum] = counts.get(spec_item.matched_stratum, 0) + 1
            strata = tuple(
                {
                    "matched_stratum": stratum,
                    "control_count": count,
                    "sufficient": count >= 12,
                }
                for stratum, count in sorted(counts.items())
            )
            return {
                "strata": strata,
                "classified_subjects": len(classification_by_subject),
                **authority,
            }
        if arguments.command == "calibration-stability-inspect":
            slices = repository.stability_slices_for_calibration(arguments.calibration_id)
            return {"stability_slices": slices, **authority}
        if arguments.command == "follow-up-requalify":
            plan = calibration_repository.plan_by_id(arguments.calibration_id)
            if plan is None:
                raise ValueError("CALIBRATION_V2_PLAN_NOT_FOUND")
            created_at = parsed(arguments.created_at)
            follow_ups = cross_market_repository.list_follow_ups()
            hypothesis_classifications = {
                item.subject_id: item
                for item in repository.classifications_for_calibration(arguments.calibration_id)
                if item.subject_kind == "HYPOTHESIS"
            }
            hypotheses_by_id = {
                item.hypothesis_id: item
                for item in cross_market_repository.hypotheses_for_agenda(plan.source_agenda_id)
            }
            qualification_results = RequalifyFollowUpsV2(
                cross_market_repository=cross_market_repository
            ).execute(
                calibration=plan,
                follow_up_ids=[item.follow_up_id for item in follow_ups],
                hypotheses_by_id=hypotheses_by_id,
                hypothesis_classifications=hypothesis_classifications,
                created_at=created_at,
            )
            for item in qualification_results:
                repository.save_follow_up_qualification(item)
            return {"follow_up_qualifications": qualification_results, **authority}
        if arguments.command == "evidence-grade-v2-inspect":
            subject_classification = repository.classification_for_subject(
                calibration_id=arguments.calibration_id, subject_id=arguments.subject_id
            )
            if subject_classification is None:
                raise ValueError("EVIDENCE_GRADE_V2_CLASSIFICATION_NOT_FOUND")
            return {"classification": subject_classification, **authority}
        raise ValueError("RESEARCH_CALIBRATION_V2_COMMAND_UNSUPPORTED")
    except CalibrationNotFound as error:
        raise ValueError(str(error)) from error
    finally:
        engine.dispose()


def _run_research_replication_command(arguments: argparse.Namespace) -> object:
    """Calibration-Qualified Interaction Replication v1 (section 23):
    bounded ``research-replication-*``/``replication-*-inspect`` commands
    over exactly the one ``CALIBRATION_QUALIFIED`` follow-up this
    milestone scopes. Never creates a Strategy, Shared Opportunity, PAPER
    order, or execution authority, and never touches Coinbase/Kalshi's
    operating state."""

    from apex.adapters.cross_market_research_postgres import (
        PostgresCrossMarketResearchRepository,
    )
    from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
    from apex.adapters.genuine_multi_asset_history_postgres import (
        PostgresGenuineMultiAssetHistoryRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_calibration_postgres import PostgresCalibrationRepository
    from apex.adapters.research_calibration_v2_postgres import PostgresCalibrationV2Repository
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.research_replication_postgres import PostgresResearchReplicationRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.research_replication import (
        FreezeReplicationSpecification,
        ReplicationNotFound,
        RunReplication,
    )

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    authority = {
        "financial_mutation_capability": "NONE",
        "strategy_authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    cross_market_repository = PostgresCrossMarketResearchRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    calibration_v1_repository = PostgresCalibrationRepository(engine)
    calibration_v2_repository = PostgresCalibrationV2Repository(engine)
    replication_repository = PostgresResearchReplicationRepository(engine)
    try:
        if arguments.command == "research-replication-create":
            created_specification = FreezeReplicationSpecification(
                cross_market_repository=cross_market_repository,
                methodology_repository=methodology_repository,
                calibration_v1_repository=calibration_v1_repository,
                calibration_v2_repository=calibration_v2_repository,
                replication_repository=replication_repository,
            ).execute(
                follow_up_id=arguments.follow_up_id,
                v2_calibration_id=arguments.v2_calibration_id,
                created_at=parsed(arguments.created_at),
            )
            return {"specification": created_specification, **authority}
        if arguments.command == "research-replication-inspect":
            specification = replication_repository.specification_by_id(arguments.replication_id)
            if specification is None:
                raise ValueError("REPLICATION_SPECIFICATION_NOT_FOUND")
            partitions = replication_repository.partitions_for_replication(arguments.replication_id)
            conclusion = replication_repository.conclusion_for_replication(arguments.replication_id)
            return {
                "specification": specification,
                "partition_count": len(partitions),
                "conclusion": conclusion,
                **authority,
            }
        if arguments.command in ("research-replication-run", "research-replication-run-resume"):
            specification = replication_repository.specification_by_id(arguments.replication_id)
            if specification is None:
                raise ValueError("REPLICATION_SPECIFICATION_NOT_FOUND")
            result = RunReplication(
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                genuine_repository=PostgresGenuineMultiAssetHistoryRepository(engine),
                crypto_repository=PostgresCryptoResearchRepository(engine),
                replication_repository=replication_repository,
            ).execute(specification=specification, created_at=parsed(arguments.created_at))
            return {
                "specification": result.specification,
                "timing": result.timing,
                "partitions": result.partitions,
                "effect_profile": result.effect_profile,
                "confound": result.confound,
                "conclusion": result.conclusion,
                **authority,
            }
        if arguments.command == "replication-stability-inspect":
            partitions = replication_repository.partitions_for_replication(arguments.replication_id)
            if not partitions:
                raise ValueError("REPLICATION_PARTITIONS_NOT_FOUND")
            return {
                "full_sample": tuple(
                    item for item in partitions if item.partition_kind.value == "FULL_SAMPLE"
                ),
                "temporal": tuple(
                    item for item in partitions if item.partition_kind.value == "TIME_PERIOD"
                ),
                "volatility_state": tuple(
                    item for item in partitions if item.partition_kind.value == "VOLATILITY_STATE"
                ),
                **authority,
            }
        if arguments.command == "replication-timing-inspect":
            timing = replication_repository.timings_for_replication(arguments.replication_id)
            if not timing:
                raise ValueError("REPLICATION_TIMING_NOT_FOUND")
            return {"timing": timing, **authority}
        if arguments.command == "replication-conclusion-inspect":
            conclusion = replication_repository.conclusion_for_replication(arguments.replication_id)
            if conclusion is None:
                raise ValueError("REPLICATION_CONCLUSION_NOT_FOUND")
            effect_profile = replication_repository.effect_profile_for_replication(
                arguments.replication_id
            )
            confound = replication_repository.confound_assessment_for_replication(
                arguments.replication_id
            )
            return {
                "conclusion": conclusion,
                "effect_profile": effect_profile,
                "confound": confound,
                **authority,
            }
        raise ValueError("RESEARCH_REPLICATION_COMMAND_UNSUPPORTED")
    except ReplicationNotFound as error:
        raise ValueError(str(error)) from error
    finally:
        engine.dispose()


def _deliver_operating_cycle_alerts(
    *, engine: Any, result: Any, executed_at: datetime
) -> tuple[dict[str, object], ...]:
    """Compose the existing, previously-unwired alert outbox
    (``application.operational_reliability.DeliverOperationalAlert``/
    ``LocalAlertOutbox``) onto a genuinely new (non-replay, non-paused)
    completed cycle -- no new scheduler, notification system, or dashboard.
    Deterministic per-cycle-per-category ``alert_id`` means a later replay
    of the identical cycle (if this guard is ever bypassed) re-affirms the
    same retained alert rather than duplicating it. A transient local-file
    write failure is swallowed per candidate (delivering diagnostics is
    best-effort and must not be mistaken for a failure of the cycle it is
    reporting on, which has already durably completed by the time this
    runs) -- but a genuine validation error (e.g. an alert category this
    domain's own closed vocabulary does not recognize) is a real defect in
    the caller and is deliberately left to propagate rather than being
    silently absorbed the way a first version of this function once did."""

    if result.idempotent_replay or result.state.value == "SKIPPED_PAUSED":
        return ()
    from pathlib import Path

    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.application.operating_cycle import derive_operating_cycle_alert_candidates
    from apex.application.operational_reliability import (
        DeliverOperationalAlert,
        LocalAlertOutbox,
    )

    candidates = derive_operating_cycle_alert_candidates(result)
    if not candidates:
        return ()
    deliverer = DeliverOperationalAlert(
        PostgresOperationalReliabilityRepository(engine),
        LocalAlertOutbox(Path(".local/operational/alerts")),
    )
    delivered: list[dict[str, object]] = []
    for candidate in candidates:
        alert_id = f"{result.cycle.cycle_id}-{candidate.category.lower().replace('_', '-')}-alert"
        if candidate.category == "OPTIONS_CAPTURE_WINDOW_MISSED" and candidate.source_evidence_ids:
            # Options Capture Temporal Reevaluation Correctness v2: unlike
            # every other category here, this one can now genuinely recur
            # within one UTC-day cycle for two DIFFERENT missed sessions --
            # e.g. an already-missed prior session surfaced at cycle
            # creation, and this exact day's own session separately closing
            # missed later too (see ``options_capture_awaits_same_day_
            # reevaluation``). A bare cycle+category id would collide (same
            # id, a genuinely different message/source_evidence_ids) and
            # raise ``OPERATIONAL_EVIDENCE_ID_CONFLICT`` on the second,
            # genuinely distinct occurrence. Folding in the candidate's own
            # source_evidence_ids (which already carries the exact missed
            # session_id) keeps a replay of the *same* missed session's
            # alert idempotent while still letting a later, genuinely
            # different missed session retain its own.
            alert_id = f"{alert_id}-{'-'.join(candidate.source_evidence_ids)}"
        try:
            alert = deliverer.execute(
                alert_id=alert_id,
                category=candidate.category,
                source_evidence_ids=candidate.source_evidence_ids,
                severity=candidate.severity,
                message=candidate.message,
                attempted_at=executed_at,
            )
        except OSError:
            continue
        delivered.append(
            {"alert_id": alert.alert_id, "category": alert.category, "status": alert.status.value}
        )
    return tuple(delivered)


def _run_supervised_operating_cycle_worker(
    *,
    engine: Any,
    repository: Any,
    source: Any,
    plan_id: str,
    runner_id: str,
    run_instance_id: str,
    once: bool,
    poll_seconds: int,
    max_cycles: int,
    authority: dict[str, object],
) -> object:
    """Bounded, manually invoked local worker over the existing exact
    ``ExecuteOperatingCycle`` planner and the existing
    ``SupervisedOperatingRunner`` lease/heartbeat machinery -- no new
    scheduler, daemon, or lease mechanism.  Mirrors the proven
    ``shadow-worker`` poll-loop shape already used elsewhere in this CLI.

    Each iteration retains one deterministic cycle identity per UTC calendar
    day (``{plan_id}-worker-{YYYYMMDD}``) pinned to that day's start-of-day
    ``as_of``, so repeated polls -- including across a restart -- replay the
    same retained cycle rather than duplicating tasks, PAPER authorizations,
    orders, or fills; a genuinely new occurrence begins only when the UTC day
    rolls over. ``poll_seconds`` is bounded at or under
    ``SupervisedOperatingRunner.LEASE_SECONDS`` so the runner's PostgreSQL
    advisory-lock lease never lapses between heartbeats while this worker is
    running, preserving exclusive ownership for the whole bounded run."""

    from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.adapters.schema_compatibility_postgres import require_compatible_schema
    from apex.application.operating_cycle import ExecuteOperatingCycle
    from apex.application.operational_reliability import SupervisedOperatingRunner

    # Migration Compatibility & Deployment Guard v1: fail closed before any
    # repository read, lease claim, or task dispatch if this worker's
    # declared schema requirement does not exactly match the database it
    # just connected to -- the real 0110 incident was exactly this worker
    # reaching a task-level query (RecordInternalStrategyEvaluation) many
    # frames deep before anything noticed the schema was stale. See
    # apex.adapters.schema_compatibility_postgres and
    # docs/MIGRATION_COMPATIBILITY_GUARD_V1.md.
    require_compatible_schema(engine, worker_name="operating-cycle-worker")

    if repository.plan(plan_id) is None:
        raise ValueError("OPERATING_PLAN_NOT_FOUND")
    if not 60 <= poll_seconds <= SupervisedOperatingRunner.LEASE_SECONDS:
        raise ValueError(
            "operating cycle worker poll seconds must be between 60 and "
            f"{SupervisedOperatingRunner.LEASE_SECONDS} (the runner's lease window)"
        )
    if not 1 <= max_cycles <= 100:
        raise ValueError("operating cycle worker max cycles must be between one and 100")

    operational_repository = PostgresOperationalReliabilityRepository(engine)
    runner = SupervisedOperatingRunner(operational_repository)
    cycle_service = ExecuteOperatingCycle(
        repository, source, ExistingCapabilityOperatingTaskExecutor(engine, source)
    )
    heartbeat = runner.start(
        runner_id=runner_id,
        run_instance_id=run_instance_id,
        plan_id=plan_id,
        at=datetime.now(tz=UTC),
    )
    processed: list[dict[str, object]] = []
    iterations = 0
    try:
        while True:
            now = datetime.now(tz=UTC)
            occurrence_as_of = now.replace(hour=0, minute=0, second=0, microsecond=0)
            cycle_id = f"{plan_id}-worker-{occurrence_as_of.strftime('%Y%m%d')}"
            result = cycle_service.execute(
                cycle_id=cycle_id,
                plan_id=plan_id,
                as_of=occurrence_as_of,
                executed_at=now,
                idempotency_key=cycle_id,
            )
            delivered_alerts = _deliver_operating_cycle_alerts(
                engine=engine, result=result, executed_at=now
            )
            iterations += 1
            heartbeat = runner.heartbeat(
                runner_id=runner_id,
                run_instance_id=run_instance_id,
                plan_id=plan_id,
                at=datetime.now(tz=UTC),
                last_cycle_id=result.cycle.cycle_id,
                due_evaluation_count=iterations,
                paused=result.state.value == "SKIPPED_PAUSED",
            )
            processed.append(
                {
                    "cycle_id": result.cycle.cycle_id,
                    "as_of": occurrence_as_of,
                    "state": result.state.value,
                    "idempotent_replay": result.idempotent_replay,
                    "task_count": len(result.tasks),
                    "next_due_at": heartbeat.next_due_at,
                    "alerts_delivered": delivered_alerts,
                }
            )
            if once or iterations >= max_cycles:
                break
            time.sleep(poll_seconds)
    finally:
        heartbeat = runner.stop(
            runner_id=runner_id,
            run_instance_id=run_instance_id,
            plan_id=plan_id,
            at=datetime.now(tz=UTC),
        )
    return {
        "runner_id": runner_id,
        "run_instance_id": run_instance_id,
        "plan_id": plan_id,
        "iterations": iterations,
        "processed": tuple(processed),
        "final_runner_status": heartbeat.status,
        "created_evidence": (),
        "reused_evidence": (),
        **authority,
    }


def _run_supervised_live_observer_worker(
    *,
    engine: Any,
    plan_id: str,
    runner_id: str,
    run_instance_id: str,
    once: bool,
    poll_seconds: int,
    max_cycles: int,
    authority: dict[str, object],
) -> object:
    """Bounded, manually invoked local worker for Live Market Observation
    Foundation v1 -- a deliberately separate, smaller supervised loop from
    ``_run_supervised_operating_cycle_worker``, not a new task bolted onto
    the 5-minute operating cycle: continuous top-of-book capture is a
    genuinely different cadence/semantic than that cycle's once-daily/once-
    weekly research tasks (see ``domain.live_market_observation``'s module
    docstring for the full rationale). Reuses ``SupervisedOperatingRunner``
    -- the exact same lease/heartbeat machinery, under a distinct
    ``runner_id``/``plan_id`` -- rather than inventing a second lease
    mechanism, and mirrors ``_run_supervised_operating_cycle_worker``'s
    poll-loop shape and bounds exactly.

    Each iteration captures one bounded round (``application.
    live_market_observation.ExecuteLiveMarketObservationRound``) over every
    instrument present PAPER execution depends on (PAPER Trial Closure
    Readiness v1 -- ``adapters.live_observation_requirements``), followed by
    the latest retained CRYPTO universe's eligible products, deduplicated by
    exact id -- so a research-universe refresh that swaps a representative
    route (BTC-USD -> BTC-USDC, 2026-09-22) can never silently starve an open
    PAPER lifecycle of evidence. Floored to the wall-clock minute
    (``window_start``), so repeated polls within the same minute --
    including across a restart -- replay the same retained round rather
    than duplicating provider requests or observation rows. If nothing is
    required and no CRYPTO universe has been captured yet, this iteration is
    a no-op rather than an error: the live observer never forces or
    duplicates the recurring cycle's universe-discovery work."""

    from apex.adapters.coinbase_universe_public import CoinbasePublicUniverseSource
    from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
    from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
    from apex.adapters.live_market_observation_postgres import (
        PostgresLiveMarketObservationRepository,
    )
    from apex.adapters.live_observation_requirements import (
        PostgresExecutionObservationRequirements,
        execute_live_observer_iteration,
    )
    from apex.adapters.operating_cycle_postgres import PostgresOperatingCycleRepository
    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.adapters.schema_compatibility_postgres import require_compatible_schema
    from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
        PostgresStrategyPaperTrialRepository,
    )
    from apex.application.live_market_observation import ExecuteLiveMarketObservationRound
    from apex.application.operational_reliability import SupervisedOperatingRunner

    # Migration Compatibility & Deployment Guard v1: see the identical
    # comment in _run_supervised_operating_cycle_worker above -- both
    # unattended workers share one exact-revision contract rather than
    # each declaring an independent minimum, since both ship from the
    # same checkout with no independent versioning of their own.
    require_compatible_schema(engine, worker_name="live-observer-worker")

    if not 60 <= poll_seconds <= SupervisedOperatingRunner.LEASE_SECONDS:
        raise ValueError(
            "live observer worker poll seconds must be between 60 and "
            f"{SupervisedOperatingRunner.LEASE_SECONDS} (the runner's lease window)"
        )
    if not 1 <= max_cycles <= 100:
        raise ValueError("live observer worker max cycles must be between one and 100")

    universe_repository = PostgresCryptoUniverseRepository(engine)
    requirements = PostgresExecutionObservationRequirements(
        trials=PostgresStrategyPaperTrialRepository(engine),
        crypto_paper=PostgresCryptoPaperRepository(engine),
        operating=PostgresOperatingCycleRepository(engine),
    )
    observation_repository = PostgresLiveMarketObservationRepository(engine)
    operational_repository = PostgresOperationalReliabilityRepository(engine)
    runner = SupervisedOperatingRunner(operational_repository)
    round_service = ExecuteLiveMarketObservationRound(
        observation_repository, CoinbasePublicUniverseSource()
    )
    heartbeat = runner.start(
        runner_id=runner_id,
        run_instance_id=run_instance_id,
        plan_id=plan_id,
        at=datetime.now(tz=UTC),
    )
    processed: list[dict[str, object]] = []
    iterations = 0
    try:
        while True:
            iteration = execute_live_observer_iteration(
                universe=universe_repository,
                requirements=requirements,
                round_service=round_service,
                plan_id=plan_id,
                now=datetime.now(tz=UTC),
            )
            last_round_id: str | None = iteration.round_id
            iterations += 1
            heartbeat = runner.heartbeat(
                runner_id=runner_id,
                run_instance_id=run_instance_id,
                plan_id=plan_id,
                at=datetime.now(tz=UTC),
                last_cycle_id=last_round_id,
                due_evaluation_count=iterations,
            )
            processed.append(
                {
                    "round_id": last_round_id,
                    "window_start": iteration.window_start,
                    "status": iteration.round_status,
                    "requested_instrument_ids": iteration.requested_instrument_ids,
                    "succeeded_count": iteration.succeeded_count,
                    "failed_count": iteration.failed_count,
                    "idempotent_replay": iteration.idempotent_replay,
                    "execution_required_instruments": tuple(
                        {
                            "instrument_id": item.instrument_id,
                            "dependencies": tuple(value.value for value in item.dependencies),
                        }
                        for item in iteration.execution_required
                    ),
                    "execution_required_missing": iteration.execution_required_missing,
                    "execution_required_resolution_error": (
                        iteration.execution_required_resolution_error
                    ),
                    "next_due_at": heartbeat.next_due_at,
                }
            )
            if once or iterations >= max_cycles:
                break
            time.sleep(poll_seconds)
    finally:
        heartbeat = runner.stop(
            runner_id=runner_id,
            run_instance_id=run_instance_id,
            plan_id=plan_id,
            at=datetime.now(tz=UTC),
        )
    return {
        "runner_id": runner_id,
        "run_instance_id": run_instance_id,
        "plan_id": plan_id,
        "iterations": iterations,
        "processed": tuple(processed),
        "final_runner_status": heartbeat.status,
        "created_evidence": (),
        "reused_evidence": (),
        **authority,
    }


def _followup_coverage_percentage(*, observed: int, considered: int) -> Decimal | None:
    """The current follow-up policy's OBSERVED share of its own
    ``considered_pairs``, as an authoritative ``Decimal`` -- never a binary
    float (Operating Daily Checkpoint Inspect Canonicalization Fix v1).

    ``observed``/``considered`` are plain ``int`` counts, and ``int / int``
    is true (binary-float) division in Python 3; the original
    ``round(100 * observed / considered, 2)`` therefore returned a genuine
    ``float``, which crashed this exact read-only checkpoint every time it
    ran -- deep inside ``_print_autopilot``'s ``canonical_json_bytes``, with
    ``domain.values._canonicalize`` correctly refusing to hash or print a
    binary float (that invariant exists for deterministic evidence and
    stays unweakened here). ``_build_operating_daily_checkpoint`` composes a
    presentation-only read model that persists nothing
    (``created_evidence: ()``), so the fix belongs at this exact point of
    production, not in the shared canonicalizer. Same "count/count as an
    exact rational, computed entirely in ``Decimal``, then quantized" idiom
    as ``percentage()`` in ``application.crypto_campaign_candidate``.

    Returns ``None`` when ``considered`` is zero (nothing has been
    considered yet, so a percentage is undefined) -- the exact same guard
    the original expression used."""

    if not considered:
        return None
    return (Decimal(observed * 100) / Decimal(considered)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )


def _build_operating_daily_checkpoint(
    *,
    engine: Any,
    repository: Any,
    plan_id: str,
    cycle_id: str,
    runner_id: str,
) -> dict[str, object]:
    """Compose one read-only daily evidence and attribution checkpoint for an
    exact retained plan/cycle out of existing retained-evidence surfaces only
    -- the crypto pulse, Kalshi operating, shared Opportunity, Crypto/Kalshi
    PAPER, Shared Performance Attribution, and operational-reliability
    repositories already used elsewhere in this CLI.  It persists nothing new,
    contacts no provider, and grants no authority; an empty PAPER performance
    section is valid and is always explained rather than left silent."""

    from apex.adapters.counterfactual_followup_postgres import (
        PostgresCounterfactualFollowUpRepository,
    )
    from apex.adapters.crypto_market_pulse_postgres import (
        PostgresCryptoMarketPulseRepository,
    )
    from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
    from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
    from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
    from apex.adapters.operating_cycle_postgres import CRYPTO_UNIVERSE_REFRESH_GENUINELY_SUPPORTED
    from apex.adapters.operating_cycle_tasks import (
        COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
        COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE,
        GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
        GENUINE_KALSHI_PAPER_ACCOUNT_ID,
    )
    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.adapters.options_quote_intelligence_postgres import (
        PostgresOptionsQuoteIntelligenceRepository,
    )
    from apex.adapters.options_research_capture import resolve_options_capture_due_decision
    from apex.adapters.options_research_operations_postgres import (
        PostgresOptionsResearchOperationsRepository,
    )
    from apex.adapters.performance_attribution_postgres import (
        PostgresPerformanceAttributionRepository,
    )
    from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
    from apex.adapters.shared_opportunity_postgres import (
        PostgresSharedOpportunityRepository,
    )
    from apex.application.counterfactual_followup import (
        PlanFollowUpObservations,
        plan_current_followup_selection,
    )
    from apex.application.crypto_paper import paper_workspace as crypto_paper_workspace
    from apex.application.kalshi_operating import kalshi_workspace
    from apex.domain.investment_mandate import DEFAULT_MANDATE_LINEAGE_ID
    from apex.domain.live_readiness import LiveRiskPolicy
    from apex.domain.operating_cycle import OperatingTaskStatus
    from apex.domain.options_research_operations import (
        MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
    )
    from apex.domain.research_evidence import EvidenceClass

    plan = repository.plan(plan_id)
    if plan is None:
        raise ValueError("OPERATING_PLAN_NOT_FOUND")
    cycle = repository.cycle(cycle_id)
    if cycle is None or cycle.plan_id != plan_id:
        raise ValueError("OPERATING_CYCLE_NOT_FOUND_FOR_EXACT_PLAN")
    controls = repository.control_events(plan_id)
    paper_activations = repository.paper_activation_events(plan_id)
    cycle_events = repository.cycle_events(cycle_id)
    task_results = repository.task_results(cycle_id)
    brief = repository.brief(f"{cycle_id}-operating-brief")
    health = repository.health(f"{cycle_id}-operating-health")
    tasks_by_type = {item.task_type.value: item for item in task_results}
    # Computed fresh from this exact cycle's own live ``task_results`` --
    # never echoed from ``brief.task_counts`` -- because ``OperatingBrief``
    # is deliberately built once per cycle (``ExecuteOperatingCycle.
    # _resume_cycle`` only calls ``build_operating_brief`` the first time a
    # cycle finalizes, typically the morning run) and never rebuilt as
    # later-resolved tasks (today: only ``OPTIONS_RESEARCH_EVIDENCE_
    # CAPTURE``) get their real outcome recorded well after that. Echoing
    # the stale brief here produced a genuinely misleading diagnostic: this
    # exact function already reads ``tasks_by_type`` live a few lines below,
    # so a reader could see a real ``FAILED`` options task next to
    # ``task_counts`` claiming zero failures. Identical formula to
    # ``domain.operating_cycle.build_operating_brief``'s own tally, just
    # evaluated against live data instead of the retained-once brief --
    # ``OperatingBrief`` itself, and every other one-per-cycle retained
    # summary, is intentionally untouched (Options Automatic Capture Parent
    # Persistence & Partial-Evidence Recovery v1, Part 13).
    live_task_counts = tuple(
        (status.value, sum(item.status is status for item in task_results))
        for status in OperatingTaskStatus
    )

    crypto_repository = PostgresCryptoMarketPulseRepository(engine)
    # Crypto Universe Recurring Refresh & Dependency Health v1: the latest
    # genuinely retained (non-FIXTURE) universe snapshot -- mirrors exactly
    # how ``PostgresOperatingStateSource.snapshot`` itself now resolves
    # ``crypto_universe_id``/``crypto_universe_created_at`` for the real due-
    # work planner, so this diagnostic can never disagree with what the
    # recurring cycle genuinely used.
    genuine_universe_snapshots = tuple(
        item
        for item in crypto_repository.universe_snapshots()
        if item.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    )
    latest_universe = genuine_universe_snapshots[-1] if genuine_universe_snapshots else None
    pulse_plan = crypto_repository.refresh_plan(plan.crypto_pulse_plan_id)
    coinbase_usable_books = None
    if brief is not None and brief.crypto_pulse_id is not None:
        pulse = crypto_repository.pulse_snapshot(brief.crypto_pulse_id)
        if pulse is not None:
            checkpoints = crypto_repository.pulse_checkpoints(pulse.refresh_id)
            coinbase_usable_books = sum(
                item.best_bid is not None
                and item.best_ask is not None
                and item.midpoint is not None
                for item in checkpoints
            )

    kalshi_repository = PostgresKalshiOperatingRepository(engine)
    kalshi_capability = kalshi_repository.latest_capability()
    kalshi_refresh = kalshi_repository.latest_refresh()

    shared_repository = PostgresSharedOpportunityRepository(engine)
    eligibility = (
        shared_repository.eligibility_decisions(brief.shared_opportunity_set_id)
        if brief is not None and brief.shared_opportunity_set_id is not None
        else ()
    )
    projection_cache: dict[str, Any] = {}

    def _projection(projection_id: str) -> Any:
        if projection_id not in projection_cache:
            projection_cache[projection_id] = shared_repository.projection(projection_id)
        return projection_cache[projection_id]

    by_strategy: dict[tuple[str, str], dict[str, object]] = {}
    rejection_reason_counts: dict[str, int] = {}
    state_totals = {"ELIGIBLE": 0, "REJECTED": 0, "EXPIRED": 0, "BLOCKED_MISSING_EVIDENCE": 0}
    for decision in eligibility:
        projection = _projection(decision.projection_id)
        key = (
            (projection.venue.value, projection.strategy_or_method_id)
            if projection is not None
            else ("UNKNOWN", "UNKNOWN")
        )
        bucket = by_strategy.setdefault(
            key,
            {
                "ELIGIBLE": 0,
                "REJECTED": 0,
                "EXPIRED": 0,
                "BLOCKED_MISSING_EVIDENCE": 0,
                "reasons": {},
            },
        )
        state_name = decision.state.value
        bucket[state_name] = cast(int, bucket[state_name]) + 1
        state_totals[state_name] = state_totals.get(state_name, 0) + 1
        for reason in decision.reasons:
            reasons = cast(dict[str, int], bucket["reasons"])
            reasons[reason] = reasons.get(reason, 0) + 1
            rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + 1
    opportunities_by_venue_and_strategy = tuple(
        {"venue": venue, "strategy_or_method_id": strategy, **detail}
        for (venue, strategy), detail in sorted(by_strategy.items())
    )

    crypto_paper_repository = PostgresCryptoPaperRepository(engine)
    crypto_paper = crypto_paper_workspace(
        crypto_paper_repository.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
    )
    kalshi_paper = kalshi_workspace(
        repository=kalshi_repository, account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID
    )

    performance_repository = PostgresPerformanceAttributionRepository(engine)
    performance = performance_repository.workspace()
    paper_evidence_counts = cast(dict[str, int], performance["paper_evidence_counts"])
    completed_paper_sample_count = paper_evidence_counts["settled"]

    operational_repository = PostgresOperationalReliabilityRepository(engine)
    runner = operational_repository.latest_runner_heartbeat(runner_id)

    live_risk_policy = LiveRiskPolicy.v1(created_at=cycle.created_at)

    control_state = controls[-1].state.value if controls else "ENABLED"
    paper_activation_state = paper_activations[-1].state.value if paper_activations else "DISABLED"
    cycle_state = cycle_events[-1].state.value if cycle_events else None
    universe_task = tasks_by_type.get("CRYPTO_UNIVERSE_REFRESH")
    crypto_pulse_task = tasks_by_type.get("CRYPTO_MARKET_PULSE")
    kalshi_task = tasks_by_type.get("KALSHI_MARKET_REFRESH")

    # Options-forward capture, portfolio refresh, and mandate evaluation
    # (Recurring Operating Cycle Integration v2): extend this one existing
    # daily checkpoint rather than adding a new dashboard. ``options_task``/
    # ``portfolio_task``/``mandate_task`` are this exact retained cycle's own
    # task results (did it happen, on this specific day); the fresh
    # ``resolve_options_capture_due_decision``/``active_mandate`` reads below
    # are deliberately "as of right now" (mirrors ``options-research-due-
    # inspect``'s own live semantics) so "what's the next eligible capture"/
    # "what mandate is active" stay correct even when this command is run
    # well after the cycle itself completed.
    options_quote_repository = PostgresOptionsQuoteIntelligenceRepository(engine)
    options_checkpoint_repository = PostgresOptionsResearchOperationsRepository(engine)
    options_task = tasks_by_type.get("OPTIONS_RESEARCH_EVIDENCE_CAPTURE")
    options_plan, _options_window_policy, _options_sessions, options_current_decision = (
        resolve_options_capture_due_decision(
            quote_repository=options_quote_repository,
            checkpoint_repository=options_checkpoint_repository,
            as_of=datetime.now(tz=UTC),
        )
    )
    options_banked_session_count = (
        0
        if options_plan is None
        else len(options_checkpoint_repository.completed_session_ids(options_plan.plan_id))
    )

    # Resolve the exact snapshot *this cycle's own task* produced or reused
    # -- never ``latest_portfolio_snapshot()``'s "most recent by as_of"
    # query. That query genuinely orders by ``as_of`` (evidence date)
    # ahead of ``created_at`` (wall-clock build time), so a manually built
    # snapshot carrying a later ``as_of`` than this cycle's own (day-scoped
    # midnight) ``as_of`` would otherwise shadow this cycle's genuinely
    # fresher snapshot -- reproduced directly against the real dev database
    # while validating this milestone. Anchoring to this task's own output
    # identity sidesteps that ambiguity entirely and matches how every
    # other section of this checkpoint already reports "this exact cycle,"
    # never "whatever is newest system-wide."
    portfolio_repository = PostgresPortfolioSnapshotRepository(engine)
    portfolio_task = tasks_by_type.get("PORTFOLIO_SNAPSHOT_REFRESH")
    portfolio_snapshot = (
        portfolio_repository.portfolio_snapshot(portfolio_task.output_identities[0])
        if portfolio_task is not None and portfolio_task.output_identities
        else None
    )

    mandate_repository = PostgresInvestmentMandateRepository(engine)
    active_mandate = mandate_repository.active_mandate(
        DEFAULT_MANDATE_LINEAGE_ID, as_of=datetime.now(tz=UTC)
    )
    mandate_task = tasks_by_type.get("MANDATE_EVALUATION_REBUILD")

    # Counterfactual Follow-Up Recurring Integration v1: this exact cycle's
    # own task result (bounded, per-cycle) alongside a fresh, read-only,
    # zero-provider-request ``PlanFollowUpObservations`` read -- deliberately
    # "as of right now", mirroring ``options_current_decision``/
    # ``active_mandate`` above, so total/due/pending counts stay correct
    # even when this diagnostic is inspected well after the cycle itself
    # completed. ``coverage()`` already carries the market/provider
    # (venue) split and the forward-vs-historical (provenance) split; both
    # are surfaced unchanged, never recomputed here.
    followup_repository = PostgresCounterfactualFollowUpRepository(engine)
    followup_task = tasks_by_type.get("COUNTERFACTUAL_FOLLOWUP_REFRESH")
    followup_coverage = followup_repository.coverage()
    followup_already_observed = {item.observation_id for item in followup_repository.observations()}
    # Counterfactual Crypto Follow-Up Point-in-Time Semantics v2: always the
    # explicit *current* policy version, matching the recurring task's own
    # choice exactly -- so this diagnostic's due/pending/observed counts
    # never silently drift from what the cycle itself is actually working
    # against.  ``followup_coverage``'s own ``observation_count_by_policy_version``
    # (surfaced separately below) is the historical, cross-version view;
    # this plan is deliberately single-version.
    followup_plan = plan_current_followup_selection(
        PlanFollowUpObservations(
            followup_repository.ledger,
            followup_repository.subjects,
            followup_repository.crypto_prices,
        ),
        as_of=datetime.now(tz=UTC),
        already_observed=followup_already_observed,
    )
    followup_state_counts = dict(followup_plan.state_counts)
    followup_due = followup_state_counts.get("DUE", 0)
    followup_pending = followup_state_counts.get("NOT_YET_DUE", 0)
    followup_observed = followup_state_counts.get("OBSERVED", 0)
    followup_unsupported = followup_state_counts.get("UNSUPPORTED", 0)
    # Counterfactual Follow-Up Starvation Fix v1: fresh, zero-provider-cost,
    # zero-write count of CRYPTO pairs this exact plan already proved can
    # never produce genuine evidence (see ``FollowUpDueState.
    # PERMANENTLY_UNAVAILABLE``) -- deliberately excluded from
    # ``followup_due`` above (it is a distinct ``FollowUpDueState``, not a
    # kind of DUE), so an operator can never mistake it for ordinary,
    # genuinely retryable backlog.
    followup_permanently_unavailable = followup_state_counts.get("PERMANENTLY_UNAVAILABLE", 0)
    followup_coverage_percentage = _followup_coverage_percentage(
        observed=followup_observed, considered=followup_plan.considered_pairs
    )

    return {
        "checkpoint_id": f"{cycle_id}-daily-checkpoint",
        "operating_plan_id": plan.plan_id,
        "operating_plan_control_state": control_state,
        "operating_plan_paper_activation_state": paper_activation_state,
        "crypto_pulse_plan_id": plan.crypto_pulse_plan_id,
        "crypto_pulse_plan_version": None if pulse_plan is None else pulse_plan.version,
        "cycle_id": cycle.cycle_id,
        "as_of": cycle.as_of,
        "cycle_state": cycle_state,
        "task_counts": live_task_counts,
        "runner_id": runner_id,
        "runner_status": None if runner is None else runner.status,
        "runner_run_instance_id": None if runner is None else runner.run_instance_id,
        "runner_plan_id": None if runner is None else runner.plan_id,
        "runner_lease_expires_at": None if runner is None else runner.lease_expires_at,
        "runner_next_due_at": None if runner is None else runner.next_due_at,
        "coinbase_requests": None
        if crypto_pulse_task is None
        else crypto_pulse_task.network_requests,
        "coinbase_usable_books": coinbase_usable_books,
        "coinbase_required_observation_types": (
            None if pulse_plan is None else pulse_plan.required_observation_types
        ),
        "kalshi_requests": None if kalshi_task is None else kalshi_task.network_requests,
        "kalshi_market_count": None if kalshi_refresh is None else kalshi_refresh.market_count,
        "kalshi_eligible_market_count": (
            None if kalshi_refresh is None else kalshi_refresh.eligible_market_count
        ),
        "kalshi_public_refresh_available": (
            None if kalshi_capability is None else kalshi_capability.public_refresh_available
        ),
        "opportunities_by_venue_and_strategy": opportunities_by_venue_and_strategy,
        "eligible_count": state_totals["ELIGIBLE"],
        "rejected_count": state_totals["REJECTED"],
        "expired_count": state_totals["EXPIRED"],
        "blocked_missing_evidence_count": state_totals["BLOCKED_MISSING_EVIDENCE"],
        "rejection_reason_counts": dict(sorted(rejection_reason_counts.items())),
        "crypto_paper": crypto_paper,
        "kalshi_paper": kalshi_paper,
        "performance_attribution": performance,
        "reconciliation_issue_count": paper_evidence_counts["reconciliation_issues"],
        "completed_genuine_paper_sample_count": completed_paper_sample_count,
        "thirty_sample_live_readiness_gate": {
            "minimum_paper_sample_count": live_risk_policy.minimum_paper_sample_count,
            "minimum_clean_fill_count": live_risk_policy.minimum_clean_fill_count,
            "minimum_settlement_count": live_risk_policy.minimum_settlement_count,
            "progress": (
                f"{completed_paper_sample_count}/{live_risk_policy.minimum_paper_sample_count}"
            ),
        },
        "empty_performance_explanation": (
            None
            if completed_paper_sample_count
            else (
                "Zero completed genuine PAPER trials exist because zero current Opportunities "
                "are ELIGIBLE this cycle (see rejection_reason_counts); PAPER orders, fills, and "
                "settlements can only accumulate once at least one genuine Opportunity clears "
                "every existing eligibility, capital, liquidity, and risk gate."
            )
        ),
        "crypto_universe": {
            "refresh_supported": CRYPTO_UNIVERSE_REFRESH_GENUINELY_SUPPORTED,
            "cycle_task_status": None if universe_task is None else universe_task.status.value,
            "cycle_task_warning_codes": (
                () if universe_task is None else universe_task.warning_codes
            ),
            "latest_snapshot_id": None if latest_universe is None else latest_universe.snapshot_id,
            "latest_snapshot_created_at": (
                None if latest_universe is None else latest_universe.created_at
            ),
            "latest_snapshot_source_capture_id": (
                None if latest_universe is None else latest_universe.source_capture_id
            ),
            "considered_product_count": (
                None if latest_universe is None else len(latest_universe.considered_product_ids)
            ),
            "eligible_product_count": (
                None if latest_universe is None else len(latest_universe.eligible_product_ids)
            ),
            "eligible_asset_ids": ()
            if latest_universe is None
            else latest_universe.eligible_asset_ids,
            "bound_pulse_plan_universe_snapshot_id": (
                None if pulse_plan is None else pulse_plan.universe_snapshot_id
            ),
        },
        "options_capture": {
            "cycle_task_status": None if options_task is None else options_task.status.value,
            "cycle_task_warning_codes": () if options_task is None else options_task.warning_codes,
            "current_due_status": options_current_decision.status.value,
            "current_session_id": options_current_decision.session_id,
            "current_window_open_at": options_current_decision.window_open_at,
            "current_window_close_at": options_current_decision.window_close_at,
            "current_reasons": options_current_decision.reasons,
            # Options Capture Temporal Reevaluation Correctness v2: additive
            # only -- surfaces the same "is there a future window still
            # ahead" fact the operating cycle itself now uses to decide
            # whether ``current_due_status`` above is provisional, without
            # changing any existing key's meaning.
            "next_capture_window_open_at": options_current_decision.next_capture_window_open_at,
            "banked_distinct_session_count": options_banked_session_count,
            "forward_evidence_cohort_size": MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
        },
        "portfolio_refresh": {
            "cycle_task_status": None if portfolio_task is None else portfolio_task.status.value,
            "cycle_task_warning_codes": (
                () if portfolio_task is None else portfolio_task.warning_codes
            ),
            "latest_snapshot_id": (
                None if portfolio_snapshot is None else portfolio_snapshot.snapshot_id
            ),
            "latest_snapshot_as_of": (
                None if portfolio_snapshot is None else portfolio_snapshot.as_of
            ),
            "latest_snapshot_completeness": (
                None if portfolio_snapshot is None else portfolio_snapshot.completeness.value
            ),
        },
        "investment_mandate": {
            "active_lineage_id": None if active_mandate is None else active_mandate.lineage_id,
            "active_version": None if active_mandate is None else active_mandate.version,
            "active_mandate_id": None if active_mandate is None else active_mandate.mandate_id,
            "cycle_task_status": None if mandate_task is None else mandate_task.status.value,
            "cycle_task_warning_codes": (
                () if mandate_task is None else mandate_task.warning_codes
            ),
        },
        "counterfactual_followup": {
            # Counterfactual Forward Outcome Temporal Integrity v1: the
            # current policy is a per-venue selection, so this reports every
            # version in it plus the venue mapping itself -- never one
            # version standing in for the whole selection.
            "policy_version": " · ".join(followup_plan.policy_versions),
            "policy_versions": list(followup_plan.policy_versions),
            "policy_version_by_venue": {
                venue: version for venue, version in followup_plan.policy_version_by_venue
            },
            "cycle_task_status": None if followup_task is None else followup_task.status.value,
            "cycle_task_warning_codes": (
                () if followup_task is None else followup_task.warning_codes
            ),
            # This exact cycle's own bounded provider-request spend -- not a
            # cumulative total (matches ``coinbase_requests``/
            # ``kalshi_requests`` above).
            "requests_consumed_this_cycle": (
                None if followup_task is None else followup_task.network_requests
            ),
            "kalshi_request_budget_per_cycle": (
                COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE
            ),
            "sample_limit_per_cycle": COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE,
            "total_pairs": followup_plan.considered_pairs,
            "due_pairs": followup_due,
            "pending_not_yet_due_pairs": followup_pending,
            "observed_pairs": followup_observed,
            "unsupported_pairs": followup_unsupported,
            # Counterfactual Follow-Up Starvation Fix v1: fresh, zero-cost,
            # zero-write, honestly distinct from ``due_pairs`` -- a CRYPTO
            # pair here can never produce genuine evidence (see
            # ``FollowUpDueState.PERMANENTLY_UNAVAILABLE``) and therefore
            # never consumes attempt or provider budget on any future cycle,
            # unlike ordinary healthy DUE backlog (Part: "must not mislabel
            # permanently impossible work as ordinary healthy DUE backlog").
            "permanently_unavailable_pairs": followup_permanently_unavailable,
            # Whether the fresh, live, genuinely-retryable DUE backlog above
            # still exceeds what one bounded cycle can attempt -- an honest
            # "this will take more than one more cycle to clear" signal
            # (Part "REQUEST BUDGETING": explicit reporting when due backlog
            # exceeds the current request budget). Never a cumulative/
            # historical count -- see ``blocked_unavailable_this_cycle``
            # below for why a true all-time "unavailable" count cannot
            # exist.  ``followup_due`` already excludes every permanently
            # unavailable pair (a distinct ``FollowUpDueState``), so this
            # never overstates the backlog a healthy cycle can actually work
            # through.
            "due_backlog_exceeds_sample_limit": followup_due
            > COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_CYCLE,
            # Only this exact cycle's own attempt can ever discover a
            # BLOCKED_UNAVAILABLE pair (a genuine, attempted read that
            # failed) -- unlike ``due``/``pending``/``observed`` above,
            # nothing about a blocked attempt is durably retained, so there
            # is no genuine cumulative figure to report instead.  This never
            # includes a permanently-unavailable pair any more
            # (Counterfactual Follow-Up Starvation Fix v1 moved that count
            # into its own ``FOLLOWUP_PERMANENTLY_UNAVAILABLE`` warning code,
            # surfaced below) -- it is therefore a more honest "genuinely
            # transient" signal than before, not merely a renamed one.
            "blocked_unavailable_this_cycle": (
                None
                if followup_task is None
                else next(
                    (
                        int(code.split(":", 1)[1])
                        for code in followup_task.warning_codes
                        if code.startswith("FOLLOWUP_BLOCKED_UNAVAILABLE:")
                    ),
                    None,
                )
            ),
            # Counterfactual Follow-Up Starvation Fix v1: this exact cycle's
            # own count of CRYPTO pairs classified PERMANENTLY_UNAVAILABLE --
            # zero attempt/provider budget spent on any of them.
            "permanently_unavailable_this_cycle": (
                None
                if followup_task is None
                else next(
                    (
                        int(code.split(":", 1)[1])
                        for code in followup_task.warning_codes
                        if code.startswith("FOLLOWUP_PERMANENTLY_UNAVAILABLE:")
                    ),
                    None,
                )
            ),
            # Counterfactual Crypto Follow-Up Point-in-Time Semantics v2: this
            # must be the *current policy's own* OBSERVED share of its own
            # considered_pairs -- ``followup_observed`` already comes from
            # this same single-version ``followup_plan`` above. Using
            # ``followup_coverage["observation_count"]`` here instead (a
            # cross-policy, all-time total) would silently conflate "how
            # much of the CURRENT policy's backlog is covered" with
            # "how many rows exist under any policy ever" -- exactly the
            # kind of decisions/policy/pair conflation this milestone exists
            # to prevent (e.g. it would read as far higher coverage than
            # the current policy genuinely has, on the day V2 goes live and
            # only V1 rows exist yet).
            # Computed above as ``followup_coverage_percentage`` -- an
            # authoritative ``Decimal``, never a binary float (Operating
            # Daily Checkpoint Inspect Canonicalization Fix v1).
            "coverage_percentage": followup_coverage_percentage,
            "market_provider_split": followup_coverage["observation_count_by_venue"],
            "forward_vs_historical_provenance_split": (
                followup_coverage["observation_count_by_provenance"]
            ),
            # Historical, cross-policy-version totals (every retained row,
            # any version) -- deliberately separate from the current-policy
            # figures above; never add these counts into `total_pairs`/
            # `due_pairs`/etc., and never read them as "current backlog".
            "observation_count_by_policy_version_all_time": (
                followup_coverage["observation_count_by_policy_version"]
            ),
            # Pre-deployment staleness-semantics audit: same historical,
            # cross-baseline-kind-caveat as the policy-version split above --
            # ORIGIN_EVIDENCE only ever exists for CRYPTO under a post-V1
            # policy version; every V1 row and every KALSHI row is
            # DECISION_TIME (Part: FollowUpBaselineKind semantics).
            "observation_count_by_baseline_kind_all_time": (
                followup_coverage["observation_count_by_baseline_kind"]
            ),
            "most_recent_observation_time": followup_coverage["most_recent_observed_at"],
        },
        "recurring_task_attention": {
            "failed_task_types": tuple(
                sorted(
                    item.task_type.value for item in task_results if item.status.value == "FAILED"
                )
            ),
            "blocked_task_types": tuple(
                sorted(
                    item.task_type.value for item in task_results if item.status.value == "BLOCKED"
                )
            ),
            "unavailable_task_types": tuple(
                sorted(
                    item.task_type.value
                    for item in task_results
                    if item.status.value == "UNAVAILABLE"
                )
            ),
        },
        "next_due_at": None if health is None else health.next_due_at,
        "operating_health_state": None if health is None else health.state.value,
        "authority": "NONE",
        "live_authority": "NONE",
        "prohibited_provider_actions": (
            "NO_COINBASE_ORDER_SUBMISSION",
            "NO_KALSHI_ORDER_SUBMISSION",
            "NO_PROVIDER_CANCELLATION_OR_REPLACEMENT",
            "NO_TRANSFER_OR_WITHDRAWAL",
            "NO_LIVE_AUTHORITY",
        ),
        "created_evidence": (),
        "reused_evidence": (cycle.cycle_id,),
    }


def _run_shared_opportunity_command(arguments: argparse.Namespace) -> object:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.shared_opportunity_postgres import (
        PostgresSharedOpportunityRepository,
    )
    from apex.adapters.shared_opportunity_sources import (
        RetainedSharedOpportunitySource,
    )
    from apex.application.shared_opportunity import (
        MaterializeSharedOpportunitySet,
        ProposeSharedResearchAllocation,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresSharedOpportunityRepository(engine)
    try:
        if arguments.command == "shared-opportunity-materialize":
            result = MaterializeSharedOpportunitySet(
                repository, RetainedSharedOpportunitySource(engine)
            ).execute(
                opportunity_set_id=arguments.opportunity_set_id,
                crypto_pulse_id=arguments.crypto_pulse_id,
                kalshi_evaluation_ids=tuple(arguments.kalshi_evaluation_id),
                as_of=datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00")),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "opportunity_set": result.opportunity_set,
                "projections": result.projections,
                "decisions": result.decisions,
                "priorities": result.priorities,
                "explanations": result.explanations,
                "idempotent_replay": result.idempotent_replay,
                "authority": "NONE",
                "financial_authority": "NONE",
                "paper_eligible": False,
                "live_eligible": False,
            }
        if arguments.command in ("shared-projection-inspect", "shared-lineage-inspect"):
            projection = repository.projection(arguments.projection_id)
            if projection is None:
                raise ValueError("SHARED_OPPORTUNITY_PROJECTION_NOT_FOUND")
            return {
                "projection": projection,
                "source_lineage": projection.supporting_evidence_ids,
                "source_details": projection.source_details,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        if arguments.command == "shared-eligibility-inspect":
            decisions = repository.eligibility_decisions(arguments.opportunity_set_id)
            if arguments.projection_id:
                decisions = tuple(
                    decision
                    for decision in decisions
                    if decision.projection_id == arguments.projection_id
                )
            return {
                "opportunity_set_id": arguments.opportunity_set_id,
                "eligibility": decisions,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        if arguments.command == "shared-opportunity-set-inspect":
            opportunity_set = repository.opportunity_set(arguments.opportunity_set_id)
            if opportunity_set is None:
                raise ValueError("SHARED_OPPORTUNITY_SET_NOT_FOUND")
            return {
                "opportunity_set": opportunity_set,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        if arguments.command == "shared-priority-inspect":
            return {
                "opportunity_set_id": arguments.opportunity_set_id,
                "priorities": repository.priorities(arguments.opportunity_set_id),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        if arguments.command == "shared-explanation-inspect":
            explanations = repository.explanations(arguments.opportunity_set_id)
            if arguments.projection_id:
                explanations = tuple(
                    explanation
                    for explanation in explanations
                    if explanation.projection_id == arguments.projection_id
                )
            return {
                "opportunity_set_id": arguments.opportunity_set_id,
                "explanations": explanations,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        if arguments.command == "shared-allocation-propose":
            return ProposeSharedResearchAllocation(repository).execute(
                proposal_id=arguments.proposal_id,
                opportunity_set_id=arguments.opportunity_set_id,
                idempotency_key=arguments.idempotency_key,
            )
        if arguments.command == "shared-allocation-inspect":
            allocation = repository.allocation(arguments.proposal_id)
            if allocation is None:
                raise ValueError("SHARED_ALLOCATION_NOT_FOUND")
            return {
                "allocation": allocation,
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        raise ValueError("SHARED_OPPORTUNITY_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_counterfactual_ledger_command(arguments: argparse.Namespace) -> object:
    """Read-only-by-default operator diagnostics for the Counterfactual
    Decision Ledger and for Counterfactual Decision Classification &
    Interpretation Versioning.  Two commands write, both idempotently and
    without ever contacting a provider or creating a mandate, portfolio
    snapshot, or Opportunity of their own: ``counterfactual-ledger-sync``
    normalizes every already-retained Shared Opportunity eligibility
    decision and Investment Mandate evaluation into
    ``counterfactual_decision`` rows (safe to re-run at any time --
    unchanged upstream evidence replays without writing anything new); and
    ``counterfactual-classification-sync`` classifies every already-retained
    ``counterfactual_decision`` row's ``raw_reason_codes`` under one named
    classifier version into ``counterfactual_decision_classification`` rows
    (same idempotent-replay guarantee; never touches
    ``counterfactual_decision`` itself)."""

    from apex.adapters.counterfactual_ledger_postgres import (
        PostgresCounterfactualLedgerRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.counterfactual_classification import (
        SyncCounterfactualClassification,
    )
    from apex.application.counterfactual_ledger import (
        SyncCounterfactualLedgerFromMandateEvaluations,
        SyncCounterfactualLedgerFromSharedOpportunity,
    )
    from apex.domain.counterfactual_classification import (
        CURRENT_CLASSIFIER_VERSION,
        KNOWN_CLASSIFIER_VERSIONS,
        unmapped_raw_reason_codes,
    )
    from apex.domain.counterfactual_ledger import (
        COUNTERFACTUAL_LEDGER_WARNING,
        DecisionDisposition,
        DecisionOrigin,
    )
    from apex.domain.shared_opportunity import SharedVenue

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCounterfactualLedgerRepository(engine)
    try:
        if arguments.command == "counterfactual-ledger-sync":
            evaluated_at = (
                datetime.now(tz=UTC)
                if arguments.evaluated_at is None
                else datetime.fromisoformat(arguments.evaluated_at.replace("Z", "+00:00"))
            )
            eligibility_result = SyncCounterfactualLedgerFromSharedOpportunity(
                repository, repository.shared_opportunity
            ).execute(evaluated_at=evaluated_at)
            mandate_result = SyncCounterfactualLedgerFromMandateEvaluations(
                repository, repository.investment_mandate
            ).execute(evaluated_at=evaluated_at)
            return {
                "eligibility_sync": eligibility_result,
                "mandate_sync": mandate_result,
                "coverage": repository.coverage(),
                "authority": "NONE",
                "warning": COUNTERFACTUAL_LEDGER_WARNING,
            }
        if arguments.command == "counterfactual-decision-inspect":
            decision = repository.decision(arguments.decision_id)
            if decision is None:
                raise ValueError("COUNTERFACTUAL_DECISION_NOT_FOUND")
            related = repository.decisions_for_subject(decision.subject_projection_id)
            classifications = repository.classifications_for_decision(decision.decision_id)
            classified_versions = {item.classifier_version for item in classifications}
            return {
                "decision": decision,
                "other_decisions_for_same_subject": tuple(
                    item for item in related if item.decision_id != decision.decision_id
                ),
                "classifications": classifications,
                "classified_under_versions": tuple(
                    item.classifier_version for item in classifications
                ),
                "unclassified_under_versions": tuple(
                    version
                    for version in KNOWN_CLASSIFIER_VERSIONS
                    if version not in classified_versions
                ),
                "current_classifier_version": CURRENT_CLASSIFIER_VERSION,
                "classified_under_current_version": CURRENT_CLASSIFIER_VERSION
                in classified_versions,
                "authority": "NONE",
                "warning": COUNTERFACTUAL_LEDGER_WARNING,
            }
        if arguments.command == "counterfactual-ledger-inspect":
            start = (
                None
                if arguments.start is None
                else datetime.fromisoformat(arguments.start.replace("Z", "+00:00"))
            )
            end = (
                None
                if arguments.end is None
                else datetime.fromisoformat(arguments.end.replace("Z", "+00:00"))
            )
            origin = None if arguments.origin is None else DecisionOrigin(arguments.origin)
            venue = None if arguments.venue is None else SharedVenue(arguments.venue)
            disposition = (
                None
                if arguments.disposition is None
                else DecisionDisposition(arguments.disposition)
            )
            decisions = repository.decisions(
                start=start, end=end, origin=origin, venue=venue, disposition=disposition
            )
            disposition_counts: dict[str, int] = {}
            origin_counts: dict[str, int] = {}
            venue_counts: dict[str, int] = {}
            raw_reason_code_counts: dict[str, int] = {}
            strategy_counts: dict[str, int] = {}
            projection_cache: dict[str, Any] = {}
            for item in decisions:
                disposition_counts[item.disposition.value] = (
                    disposition_counts.get(item.disposition.value, 0) + 1
                )
                origin_counts[item.origin.value] = origin_counts.get(item.origin.value, 0) + 1
                venue_counts[item.venue.value] = venue_counts.get(item.venue.value, 0) + 1
                for raw in item.raw_reason_codes:
                    raw_reason_code_counts[raw] = raw_reason_code_counts.get(raw, 0) + 1
                if item.subject_projection_id not in projection_cache:
                    projection_cache[item.subject_projection_id] = (
                        repository.shared_opportunity.projection(item.subject_projection_id)
                    )
                projection = projection_cache[item.subject_projection_id]
                strategy_id = (
                    "UNKNOWN_PROJECTION_MISSING"
                    if projection is None
                    else projection.strategy_or_method_id
                )
                strategy_counts[strategy_id] = strategy_counts.get(strategy_id, 0) + 1
            return {
                "filters": {
                    "start": start,
                    "end": end,
                    "origin": None if origin is None else origin.value,
                    "venue": None if venue is None else venue.value,
                    "disposition": None if disposition is None else disposition.value,
                },
                "considered_count": len(decisions),
                "disposition_counts": disposition_counts,
                "origin_counts": origin_counts,
                "venue_counts": venue_counts,
                "raw_reason_code_counts": raw_reason_code_counts,
                "strategy_or_method_counts": strategy_counts,
                "reason_family_classification_note": (
                    "this decision-only diagnostic reports raw_reason_code_counts, never a "
                    "reason-family breakdown -- reason families are an evolvable, versioned "
                    "interpretation of raw codes, not a decision fact; see "
                    "counterfactual-classification-inspect/-coverage for that, and "
                    "counterfactual-decision-inspect for one decision's own retained "
                    "classifications across every classifier version"
                ),
                "coverage": repository.coverage(),
                "decisions": decisions,
                "authority": "NONE",
                "warning": COUNTERFACTUAL_LEDGER_WARNING,
            }
        if arguments.command == "counterfactual-classification-sync":
            classified_at = (
                datetime.now(tz=UTC)
                if arguments.classified_at is None
                else datetime.fromisoformat(arguments.classified_at.replace("Z", "+00:00"))
            )
            classifier_version = arguments.classifier_version or CURRENT_CLASSIFIER_VERSION
            result = SyncCounterfactualClassification(repository, repository).execute(
                classified_at=classified_at, classifier_version=classifier_version
            )
            return {
                "classification_sync": result,
                "classification_coverage": repository.classification_coverage(
                    classifier_version=classifier_version
                ),
                "authority": "NONE",
                "warning": COUNTERFACTUAL_LEDGER_WARNING,
            }
        if arguments.command == "counterfactual-classification-inspect":
            start = (
                None
                if arguments.start is None
                else datetime.fromisoformat(arguments.start.replace("Z", "+00:00"))
            )
            end = (
                None
                if arguments.end is None
                else datetime.fromisoformat(arguments.end.replace("Z", "+00:00"))
            )
            classifier_version = arguments.classifier_version
            classifications = repository.classifications(
                classifier_version=classifier_version, start=start, end=end
            )
            version_counts: dict[str, int] = {}
            family_occurrence_counts_by_version: dict[str, dict[str, int]] = {}
            for classification in classifications:
                version_counts[classification.classifier_version] = (
                    version_counts.get(classification.classifier_version, 0) + 1
                )
                bucket = family_occurrence_counts_by_version.setdefault(
                    classification.classifier_version, {}
                )
                for family in classification.reason_families:
                    bucket[family.value] = bucket.get(family.value, 0) + 1
            return {
                "filters": {"classifier_version": classifier_version, "start": start, "end": end},
                "considered_count": len(classifications),
                "classification_row_note": (
                    "these are versioned classification/interpretation rows, not decision "
                    "events -- do not add this considered_count to counterfactual-ledger-"
                    "inspect's; the same decision classified under two versions is still one "
                    "decision event and two classification rows"
                ),
                "version_counts": version_counts,
                "reason_family_occurrence_counts_by_version": family_occurrence_counts_by_version,
                "reason_family_occurrence_note": (
                    "one classification may carry more than one reason family; these counts "
                    "are occurrences, not a partition, and will not sum to that version's own "
                    "count in version_counts"
                ),
                "known_classifier_versions": KNOWN_CLASSIFIER_VERSIONS,
                "current_classifier_version": CURRENT_CLASSIFIER_VERSION,
                "classifications": classifications,
                "authority": "NONE",
                "warning": COUNTERFACTUAL_LEDGER_WARNING,
            }
        if arguments.command == "counterfactual-classification-coverage":
            classifier_version = arguments.classifier_version or CURRENT_CLASSIFIER_VERSION
            coverage = repository.classification_coverage(classifier_version=classifier_version)
            unmapped_counts: dict[str, int] = {}
            for item in repository.all_decisions():
                for raw in unmapped_raw_reason_codes(
                    item.raw_reason_codes, classifier_version=classifier_version
                ):
                    unmapped_counts[raw] = unmapped_counts.get(raw, 0) + 1
            return {
                "coverage": coverage,
                "unmapped_raw_reason_code_counts": unmapped_counts,
                "unmapped_note": (
                    "distinct raw reason codes, across every retained decision, that "
                    "classifier_version does not explicitly map (no exact-match entry and not "
                    "an explicitly-excluded structural marker) -- these fall through to the "
                    "generic keyword fallback or all the way to OTHER_EXPLICIT; counts are "
                    "occurrences across decisions, not distinct-code counts"
                ),
                "known_classifier_versions": KNOWN_CLASSIFIER_VERSIONS,
                "authority": "NONE",
                "warning": COUNTERFACTUAL_LEDGER_WARNING,
            }
        raise ValueError("COUNTERFACTUAL_LEDGER_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_external_strategy_research_command(arguments: argparse.Namespace) -> object:
    """External Strategy Evaluation v1: register a declarative external
    strategy specification, deterministically evaluate it against retained
    Apex candle evidence, and inspect the resulting registry -- entirely
    independent of, and never wired into, the live recurring
    ``STRATEGY_PAPER_TRIAL_EXECUTION`` task or its own fixed benchmark
    catalog. ``external-strategy-promote`` is the one explicit action that
    can move a definition to ``PAPER_ELIGIBLE``; nothing here ever
    authorizes, previews, or submits a PAPER order (see
    ``application.external_strategy_research``'s own module docstring)."""

    import json

    from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository
    from apex.adapters.external_strategy_research_postgres import (
        PostgresExternalStrategyDefinitionRepository,
        PostgresExternalStrategyEvaluationRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.strategy_version_provenance_postgres import (
        PostgresStrategyVersionProvenanceRepository,
    )
    from apex.application.external_strategy_research import (
        EvaluateExternalStrategy,
        ExternalStrategyResearchError,
        PromoteExternalStrategy,
        RegisterExternalStrategy,
        external_strategy_detail,
        external_strategy_registry_snapshot,
    )
    from apex.domain.external_strategy import (
        EXTERNAL_STRATEGY_WARNING,
        ExternalStrategyEvaluationError,
        ExternalStrategySpecificationError,
    )

    def _parsed(value: str | None) -> datetime:
        return (
            datetime.now(tz=UTC)
            if value is None
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )

    engine = create_postgres_engine(PostgresSettings.from_env())
    definitions = PostgresExternalStrategyDefinitionRepository(engine)
    evaluations = PostgresExternalStrategyEvaluationRepository(engine)
    try:
        if arguments.command == "external-strategy-register":
            try:
                with open(arguments.spec_file, encoding="utf-8") as handle:
                    specification = json.load(handle)
            except json.JSONDecodeError as error:
                raise ExternalStrategySpecificationError(
                    f"EXTERNAL_STRATEGY_SPEC_FILE_NOT_VALID_JSON:{error}"
                ) from error
            register_result = RegisterExternalStrategy(definitions).execute(
                specification=specification, created_at=_parsed(arguments.registered_at)
            )
            return {
                "definition": register_result.definition,
                "idempotent_replay": register_result.idempotent_replay,
                "authority": "NONE",
                "warning": EXTERNAL_STRATEGY_WARNING,
            }
        if arguments.command == "external-strategy-evaluate":
            version = arguments.version
            if version is None:
                latest = definitions.latest_version(strategy_id=arguments.strategy_id)
                if latest is None:
                    raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_DEFINITION_NOT_FOUND")
                version = latest.version
            candles = PostgresCryptoResearchRepository(engine)
            evaluate_result = EvaluateExternalStrategy(
                definitions=definitions, evaluations=evaluations, candles=candles
            ).execute(
                strategy_id=arguments.strategy_id,
                version=version,
                capture_id=arguments.capture_id,
                as_of=datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00")),
                evaluated_at=_parsed(arguments.evaluated_at),
            )
            return {
                "evaluation": evaluate_result.record,
                "idempotent_replay": evaluate_result.idempotent_replay,
                "compared_benchmark_strategy_ids": evaluate_result.compared_benchmark_strategy_ids,
                "skipped_benchmark_strategy_ids": evaluate_result.skipped_benchmark_strategy_ids,
                "authority": "NONE",
                "paper_authority": "NONE",
                "warning": EXTERNAL_STRATEGY_WARNING,
            }
        if arguments.command == "external-strategy-promote":
            promoter = PromoteExternalStrategy(
                definitions=definitions,
                evaluations=evaluations,
                provenance=PostgresStrategyVersionProvenanceRepository(engine),
            )
            promote_result = promoter.execute(
                strategy_id=arguments.strategy_id,
                version=arguments.version,
                evaluation_id=arguments.evaluation_id,
                promoted_at=_parsed(arguments.promoted_at),
            )
            return {
                "definition": promote_result.definition,
                "idempotent_replay": promote_result.idempotent_replay,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
                "warning": (
                    "PROMOTION CHANGES ONLY lifecycle_state -- IT NEVER AUTHORIZES, "
                    "PREVIEWS, OR SUBMITS A PAPER ORDER"
                ),
            }
        if arguments.command == "external-strategy-inspect":
            return external_strategy_detail(
                definitions=definitions, evaluations=evaluations, strategy_id=arguments.strategy_id
            )
        if arguments.command == "external-strategy-list":
            return external_strategy_registry_snapshot(
                definitions=definitions, evaluations=evaluations
            )
        raise ValueError("EXTERNAL_STRATEGY_RESEARCH_COMMAND_UNSUPPORTED")
    except (
        ExternalStrategySpecificationError,
        ExternalStrategyResearchError,
        ExternalStrategyEvaluationError,
    ) as error:
        raise SystemExit(str(error)) from error
    finally:
        engine.dispose()


def _run_internal_strategy_history_command(arguments: argparse.Namespace) -> object:
    """Strategy Evaluation History & Promotion Lineage v1: read-only
    inspection of the durably retained evaluation/promotion lineage for
    Apex's OWN recurring ``STRATEGY_PAPER_TRIAL_EXECUTION`` catalog, plus
    the one explicit, operator-invoked ``internal-strategy-promote``
    action. Never authorizes, previews, or submits a PAPER order -- see
    ``application.strategy_evaluation_history``'s own module docstring."""

    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.strategy_evaluation_history_postgres import (
        PostgresInternalStrategyEvaluationRepository,
        PostgresInternalStrategyPromotionRepository,
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
    from apex.application.strategy_evaluation_history import (
        STRATEGY_EVALUATION_HISTORY_WARNING,
        PromoteInternalStrategy,
        StrategyEvaluationHistoryResearchError,
        internal_strategy_evaluation_history,
        internal_strategy_evaluation_registry,
    )
    from apex.application.strategy_trial_outcome_assessment import (
        strategy_trial_outcome_report,
        strategy_trial_outcome_summary,
    )
    from apex.application.strategy_version_provenance import (
        StrategyVersionProvenanceResearchError,
        strategy_version_provenance_report,
    )
    from apex.domain.strategy_benchmark import canonical_strategy_benchmarks
    from apex.domain.strategy_evaluation_history import StrategyEvaluationHistoryError
    from apex.domain.strategy_trial_outcome_assessment import (
        StrategyTrialOutcomeAssessment,
        StrategyTrialOutcomeAssessmentError,
    )
    from apex.domain.strategy_version_provenance import (
        STRATEGY_VERSION_PROVENANCE_WARNING,
        StrategyVersionProvenanceError,
    )

    def _parsed(value: str | None) -> datetime:
        return (
            datetime.now(tz=UTC)
            if value is None
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )

    engine = create_postgres_engine(PostgresSettings.from_env())
    evaluations = PostgresInternalStrategyEvaluationRepository(engine)
    promotions = PostgresInternalStrategyPromotionRepository(engine)
    provenance = PostgresStrategyVersionProvenanceRepository(engine)
    try:
        if arguments.command == "internal-strategy-list":
            now = datetime.now(tz=UTC)
            strategy_ids = tuple(
                item.strategy_id for item in canonical_strategy_benchmarks(created_at=now)
            )
            return {
                "strategies": internal_strategy_evaluation_registry(
                    evaluations=evaluations, promotions=promotions, strategy_ids=strategy_ids
                ),
                "authority": "NONE",
                "warning": STRATEGY_EVALUATION_HISTORY_WARNING,
            }
        if arguments.command == "internal-strategy-inspect":
            return internal_strategy_evaluation_history(
                evaluations=evaluations, promotions=promotions, strategy_id=arguments.strategy_id
            )
        if arguments.command == "internal-strategy-promote":
            promote_result = PromoteInternalStrategy(
                evaluations=evaluations, promotions=promotions, provenance=provenance
            ).execute(
                strategy_id=arguments.strategy_id,
                evaluation_id=arguments.evaluation_id,
                promoted_at=_parsed(arguments.promoted_at),
                actor=arguments.actor,
            )
            return {
                "promotion": promote_result.promotion,
                "idempotent_replay": promote_result.idempotent_replay,
                "authority": "NONE",
                "paper_authority": "NONE",
                "live_authority": "NONE",
                "warning": (
                    "PROMOTION RECORDS LINEAGE ONLY -- IT NEVER AUTHORIZES, PREVIEWS, "
                    "OR SUBMITS A PAPER ORDER, AND NEVER CHANGES WHAT THE LIVE RECURRING "
                    "CYCLE TRADES"
                ),
            }
        if arguments.command == "internal-strategy-trial-outcomes":
            outcome_report = strategy_trial_outcome_report(
                assessments=PostgresStrategyTrialOutcomeAssessmentRepository(engine).assessments(),
                closures=PostgresStrategyPaperTrialClosureRepository(engine).all_closures(),
                active_trials=PostgresStrategyPaperTrialRepository(engine).active_trials(),
                strategy_id=arguments.strategy_id,
                trial_id=arguments.trial_id,
            )
            return {
                **outcome_report,
                "summary": tuple(
                    strategy_trial_outcome_summary(item)
                    for item in cast(
                        "tuple[StrategyTrialOutcomeAssessment, ...]", outcome_report["assessments"]
                    )
                ),
            }
        if arguments.command == "internal-strategy-provenance":
            report = strategy_version_provenance_report(
                provenance=provenance,
                strategy_id=arguments.strategy_id,
                trials=PostgresStrategyPaperTrialRepository(engine).for_strategy(
                    strategy_id=arguments.strategy_id
                ),
            )
            return {**report, "warning": STRATEGY_VERSION_PROVENANCE_WARNING}
        raise ValueError("INTERNAL_STRATEGY_HISTORY_COMMAND_UNSUPPORTED")
    except (
        StrategyEvaluationHistoryError,
        StrategyEvaluationHistoryResearchError,
        StrategyVersionProvenanceError,
        StrategyVersionProvenanceResearchError,
        StrategyTrialOutcomeAssessmentError,
    ) as error:
        raise SystemExit(str(error)) from error
    finally:
        engine.dispose()


def _run_evidence_gap_command(arguments: argparse.Namespace) -> object:
    """Historical Evidence Gap Recovery v1 operator surface. ``preview`` is
    read-only; ``detect`` records irrecoverable gaps with zero provider
    requests; ``recover`` performs bounded read-only provider history GETs for
    daily securities bars / crypto candles only. Authority is always NONE."""

    from apex.adapters.evidence_recovery_postgres import PostgresEvidenceRecoveryRepository
    from apex.adapters.evidence_recovery_runner import (
        build_crypto_fetcher,
        build_securities_fetcher,
        preview_gaps,
        run_evidence_gap_recovery,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.domain.evidence_recovery import EvidenceFamily

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresEvidenceRecoveryRepository(engine)
        if arguments.command == "evidence-gap-inspect":
            family = None if arguments.family is None else EvidenceFamily(arguments.family)
            gaps = repository.current_gaps(evidence_family=family)
            counts: dict[str, int] = {}
            for gap in gaps:
                label = f"{gap.evidence_family.value}:{gap.disposition.value}"
                counts[label] = counts.get(label, 0) + 1
            return {
                "gap_counts": counts,
                "gaps": gaps[: arguments.limit],
                "recovery_runs": repository.runs()[-10:],
                "vintage_count": repository.vintage_count(),
                "authority": "NONE",
            }
        if arguments.command == "evidence-gap-vintage-path-correct":
            pending = repository.mislabelled_recovery_vintages()
            written = 0
            if arguments.apply:
                written = repository.correct_acquisition_paths(
                    recorded_at=datetime.now(tz=UTC), expected_count=arguments.expected_count
                )
            return {
                "mode": "apply" if arguments.apply else "dry_run",
                "mislabelled_found": len(pending),
                "run_ids": sorted({str(item.recovery_run_id) for item in pending}),
                "corrections_written": written,
                "provider_requests": 0,
                "authority": "NONE",
            }
        if arguments.command == "evidence-gap-recovery-run":
            now = datetime.now(tz=UTC)
            as_of = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if arguments.mode == "preview":
                return {"mode": "preview", **preview_gaps(engine=engine, as_of=now, now=now)}
            from apex.adapters.crypto_market_pulse_postgres import (
                PostgresCryptoMarketPulseRepository,
            )

            crypto = PostgresCryptoMarketPulseRepository(engine)
            report = run_evidence_gap_recovery(
                engine=engine,
                as_of=now,
                executed_at=now,
                cycle_key=arguments.cycle_key or f"operator-{as_of.date().isoformat()}",
                securities_fetcher=build_securities_fetcher(engine),
                crypto_fetcher=build_crypto_fetcher(crypto),
                crypto_repository=crypto,
                detect_only=arguments.mode == "detect",
            )
            return {"mode": arguments.mode, "authority": "NONE", **report.as_dict()}
        raise ValueError("EVIDENCE_GAP_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_counterfactual_followup_command(arguments: argparse.Namespace) -> object:
    """Read-only-by-default operator diagnostics for Decision Follow-Up
    Observation v1.  One command writes, idempotently, and only ever
    performs bounded, read-only, unauthenticated genuine public reads (never
    a provider mutation, never PAPER/LIVE authority): ``followup-sync``
    attempts at most ``--sample-limit`` genuine observations for decisions
    currently ``DUE``, skipping everything not due at zero cost.
    ``followup-plan`` is fully read-only and makes no provider request at
    all -- see ``application.counterfactual_followup`` for the full
    due/observed/blocked/unsupported state contract."""

    from apex.adapters.counterfactual_followup_postgres import (
        PostgresCounterfactualFollowUpRepository,
    )
    from apex.adapters.counterfactual_followup_sources import KalshiFollowUpPublicMarketSource
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.counterfactual_followup import (
        DEFAULT_SAMPLE_LIMIT,
        DEFAULT_TIMEOUT_SECONDS,
        AssessFollowUpEvidenceQuality,
        PlanFollowUpObservations,
        SyncFollowUpObservations,
        plan_current_followup_selection,
    )
    from apex.domain.counterfactual_followup import (
        COUNTERFACTUAL_FOLLOWUP_WARNING,
        CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
        CURRENT_FOLLOWUP_POLICY_VERSIONS,
        FOLLOWUP_EVIDENCE_QUALITY_WARNING,
        FollowUpBaselineKind,
        FollowUpEvidenceQualityAssessment,
        FollowUpEvidenceQualityVerdict,
        FollowUpHorizonKind,
        current_followup_evidence_quality_policy_version,
        current_followup_policy_venues,
    )
    from apex.domain.shared_opportunity import SharedVenue

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCounterfactualFollowUpRepository(engine)
    try:
        if arguments.command == "followup-plan":
            as_of = (
                datetime.now(tz=UTC)
                if arguments.as_of is None
                else datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00"))
            )
            already_observed = {item.observation_id for item in repository.observations()}
            plan_service = PlanFollowUpObservations(
                repository.ledger, repository.subjects, repository.crypto_prices
            )
            # Counterfactual Forward Outcome Temporal Integrity v1: with no
            # explicit ``--policy-version``, plan the whole *current
            # selection* (one bounded read-only plan per distinct version,
            # each scoped to its own venues) rather than silently choosing
            # one of its versions and hiding the other's venues.  An
            # explicit ``--policy-version`` still plans exactly that one
            # version across every venue, unchanged -- that override exists
            # precisely so an operator can inspect a non-current version's
            # own backlog.
            if arguments.policy_version is None:
                selection = plan_current_followup_selection(
                    plan_service, as_of=as_of, already_observed=already_observed
                )
                return {
                    "as_of": selection.as_of,
                    "policy_version": " · ".join(selection.policy_versions),
                    "policy_versions": list(selection.policy_versions),
                    "policy_version_by_venue": {
                        venue: version for venue, version in selection.policy_version_by_venue
                    },
                    "considered_decisions": selection.considered_decisions,
                    "considered_pairs": selection.considered_pairs,
                    "state_counts": dict(selection.state_counts),
                    "state_counts_by_venue": {
                        venue: dict(counts) for venue, counts in selection.state_counts_by_venue
                    },
                    "note": (
                        "read-only; makes no provider request and writes nothing -- see "
                        "followup-sync to genuinely execute a bounded sample of what this "
                        "plan reports as DUE; this is the current per-venue policy selection, "
                        "whose versions cover disjoint venues, so considered_pairs is a "
                        "genuine total and not a double count -- pass --policy-version to "
                        "inspect one version's own backlog across every venue instead"
                    ),
                    "authority": "NONE",
                    "warning": COUNTERFACTUAL_FOLLOWUP_WARNING,
                }
            plan = plan_service.execute(
                as_of=as_of,
                already_observed=already_observed,
                policy_version=arguments.policy_version,
            )
            return {
                "as_of": plan.as_of,
                "policy_version": plan.policy_version,
                "considered_decisions": plan.considered_decisions,
                "considered_pairs": plan.considered_pairs,
                "state_counts": dict(plan.state_counts),
                "state_counts_by_venue": {
                    venue: dict(counts) for venue, counts in plan.state_counts_by_venue
                },
                "note": (
                    "read-only; makes no provider request and writes nothing -- see "
                    "followup-sync to genuinely execute a bounded sample of what this plan "
                    "reports as DUE; considered_pairs reflects this one policy version only "
                    "-- omit --policy-version for the current per-venue selection, do not "
                    "sum multiple runs together"
                ),
                "authority": "NONE",
                "warning": COUNTERFACTUAL_FOLLOWUP_WARNING,
            }
        if arguments.command == "followup-sync":
            as_of = (
                datetime.now(tz=UTC)
                if arguments.as_of is None
                else datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00"))
            )
            sample_limit = (
                DEFAULT_SAMPLE_LIMIT if arguments.sample_limit is None else arguments.sample_limit
            )
            timeout_seconds = (
                DEFAULT_TIMEOUT_SECONDS
                if arguments.timeout_seconds is None
                else arguments.timeout_seconds
            )
            kalshi_source = KalshiFollowUpPublicMarketSource()
            sync_service = SyncFollowUpObservations(
                repository.ledger,
                repository.subjects,
                repository.crypto_prices,
                kalshi_source,
                repository,
            )
            # Counterfactual Forward Outcome Temporal Integrity v1: mirrors
            # ``followup-plan`` above and the recurring cycle task exactly --
            # no explicit ``--policy-version`` runs the current per-venue
            # selection, one bounded venue-scoped run per distinct version;
            # an explicit one runs exactly that version across every venue
            # it structurally supports.  ``sample_limit`` is deliberately
            # applied per run here rather than split as the recurring cycle
            # splits it: this is an explicitly invoked, attended command
            # whose whole purpose is "do a bounded amount of work now", and
            # an operator who asked for N samples of each version's backlog
            # should get them.
            policy_versions = (
                CURRENT_FOLLOWUP_POLICY_VERSIONS
                if arguments.policy_version is None
                else (arguments.policy_version,)
            )
            results = [
                sync_service.execute(
                    as_of=as_of,
                    sample_limit=sample_limit,
                    timeout_seconds=timeout_seconds,
                    policy_version=policy_version,
                    venues=(
                        current_followup_policy_venues(policy_version)
                        if arguments.policy_version is None
                        else None
                    ),
                    measurement_as_of=as_of,
                )
                for policy_version in policy_versions
            ]
            return {
                # Kept singular for the single-version case so an existing
                # reader of this command's output is unaffected; the full
                # per-version list is always available alongside it.
                "sync_result": results[0] if len(results) == 1 else None,
                "sync_results": results,
                "coverage": repository.coverage(),
                "authority": "NONE",
                "warning": COUNTERFACTUAL_FOLLOWUP_WARNING,
            }
        if arguments.command == "followup-inspect":
            venue = None if arguments.venue is None else SharedVenue(arguments.venue)
            horizon_kind = (
                None
                if arguments.horizon_kind is None
                else FollowUpHorizonKind(arguments.horizon_kind)
            )
            baseline_kind = (
                None
                if arguments.baseline_kind is None
                else FollowUpBaselineKind(arguments.baseline_kind)
            )
            policy_version = arguments.policy_version
            start = (
                None
                if arguments.start is None
                else datetime.fromisoformat(arguments.start.replace("Z", "+00:00"))
            )
            end = (
                None
                if arguments.end is None
                else datetime.fromisoformat(arguments.end.replace("Z", "+00:00"))
            )
            observations = repository.observations(
                venue=venue,
                horizon_kind=horizon_kind,
                baseline_kind=baseline_kind,
                start=start,
                end=end,
                policy_version=policy_version,
            )
            return {
                "filters": {
                    "venue": None if venue is None else venue.value,
                    "horizon_kind": None if horizon_kind is None else horizon_kind.value,
                    "baseline_kind": None if baseline_kind is None else baseline_kind.value,
                    "policy_version": policy_version,
                    "start": start,
                    "end": end,
                },
                "considered_count": len(observations),
                "observations": observations,
                "coverage": repository.coverage(),
                "authority": "NONE",
                "warning": COUNTERFACTUAL_FOLLOWUP_WARNING,
            }
        if arguments.command == "followup-decision-inspect":
            decision = repository.ledger.decision(arguments.decision_id)
            if decision is None:
                raise ValueError("COUNTERFACTUAL_FOLLOWUP_DECISION_NOT_FOUND")
            return {
                "decision": decision,
                "classification_history": repository.ledger.classifications_for_decision(
                    decision.decision_id
                ),
                "followup_observations": repository.observations_for_decision(decision.decision_id),
                "note": (
                    "classifier evolution and follow-up-policy evolution are both independent "
                    "of decision identity -- none of the rows above ever rewrite `decision`"
                ),
                "authority": "NONE",
                "warning": COUNTERFACTUAL_FOLLOWUP_WARNING,
            }
        if arguments.command == "followup-evidence-quality-assess":
            as_of = (
                datetime.now(tz=UTC)
                if arguments.as_of is None
                else datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00"))
            )
            assessment_policy_version = (
                CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION
                if arguments.assessment_policy_version is None
                else arguments.assessment_policy_version
            )
            assessment_result = AssessFollowUpEvidenceQuality(repository, repository).execute(
                assessed_at=as_of, assessment_policy_version=assessment_policy_version
            )
            return {
                "assessment_result": assessment_result,
                "note": (
                    "additive and idempotent -- never rewrites, deletes, or replaces the "
                    "assessed decision_followup_observation rows; re-running with an "
                    "unchanged retained follow-up backlog reproduces the identical "
                    "assessment_id/canonical_hash for every row and writes nothing new"
                ),
                "authority": "NONE",
                "warning": FOLLOWUP_EVIDENCE_QUALITY_WARNING,
            }
        if arguments.command == "followup-evidence-quality-inspect":
            assessment_policy_version = arguments.assessment_policy_version
            if arguments.observation_id is not None:
                # Securities Follow-Up Temporal Integrity v1: default to the
                # observation's own venue's canonical version (V2 for
                # SECURITIES), not the all-venue V1 baseline.
                inspected = repository.observation(arguments.observation_id)
                resolved_version = assessment_policy_version or (
                    CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION
                    if inspected is None
                    else current_followup_evidence_quality_policy_version(inspected.venue)
                )
                single = repository.assessment(
                    observation_id=arguments.observation_id,
                    assessment_policy_version=resolved_version,
                )
                assessments: tuple[FollowUpEvidenceQualityAssessment, ...] = (
                    () if single is None else (single,)
                )
            else:
                assessments = repository.assessments(
                    assessment_policy_version=assessment_policy_version
                )
                if arguments.verdict is not None:
                    wanted = FollowUpEvidenceQualityVerdict(arguments.verdict)
                    assessments = tuple(item for item in assessments if item.verdict is wanted)
            return {
                "filters": {
                    "observation_id": arguments.observation_id,
                    "verdict": arguments.verdict,
                    "assessment_policy_version": assessment_policy_version,
                },
                "considered_count": len(assessments),
                "assessments": assessments,
                "authority": "NONE",
                "warning": FOLLOWUP_EVIDENCE_QUALITY_WARNING,
            }
        raise ValueError("COUNTERFACTUAL_FOLLOWUP_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_total_market_context_command(arguments: argparse.Namespace) -> object:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.total_market_context_postgres import (
        PostgresTotalMarketContextRepository,
    )
    from apex.adapters.total_market_context_sources import (
        RetainedTotalMarketContextSource,
    )
    from apex.application.total_market_context import (
        AttachTotalMarketContext,
        MaterializeTotalMarketContextSnapshot,
    )
    from apex.domain.total_market_context import (
        POLICY_THRESHOLDS,
        TOTAL_MARKET_CONTEXT_POLICY_HASH,
        TOTAL_MARKET_CONTEXT_POLICY_V1,
    )

    authority = {
        "evidence_class": "RETAINED_CONTEXT",
        "context_authority": "NONE",
        "opportunity_authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "order_authority": "NONE",
        "financial_mutation_capability": "NONE",
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresTotalMarketContextRepository(engine)
    try:
        if arguments.command == "context-snapshot-materialize":
            snapshot_result = MaterializeTotalMarketContextSnapshot(
                repository,
                RetainedTotalMarketContextSource(engine),
            ).execute(
                snapshot_id=arguments.snapshot_id,
                source_batch_id=arguments.source_batch_id,
                as_of=datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00")),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "context_snapshot": snapshot_result.snapshot,
                "components": snapshot_result.components,
                "idempotent_replay": snapshot_result.idempotent_replay,
                **authority,
            }
        if arguments.command == "context-component-inspect":
            component = repository.context_component(arguments.component_id)
            if component is None:
                raise ValueError("TOTAL_MARKET_CONTEXT_COMPONENT_NOT_FOUND")
            return {"component": component, **authority}
        if arguments.command == "context-snapshot-inspect":
            snapshot = repository.context_snapshot(arguments.snapshot_id)
            if snapshot is None:
                raise ValueError("TOTAL_MARKET_CONTEXT_SNAPSHOT_NOT_FOUND")
            return {
                "context_snapshot": snapshot,
                "components": repository.context_components(snapshot.snapshot_id),
                **authority,
            }
        if arguments.command == "context-policy-inspect":
            return {
                "policy_id": TOTAL_MARKET_CONTEXT_POLICY_V1,
                "policy_hash": TOTAL_MARKET_CONTEXT_POLICY_HASH,
                "thresholds": POLICY_THRESHOLDS,
                "prediction_claim": False,
                **authority,
            }
        if arguments.command == "opportunity-context-attach":
            enrichment_result = AttachTotalMarketContext(repository).execute(
                enriched_set_id=arguments.enriched_set_id,
                opportunity_set_id=arguments.opportunity_set_id,
                context_snapshot_id=arguments.context_snapshot_id,
                attached_at=datetime.fromisoformat(arguments.attached_at.replace("Z", "+00:00")),
                idempotency_key=arguments.idempotency_key,
            )
            return {
                "enriched_opportunity_set": enrichment_result.enriched_set,
                "attachments": enrichment_result.attachments,
                "idempotent_replay": enrichment_result.idempotent_replay,
                **authority,
            }
        if arguments.command == "contextual-explanation-inspect":
            attachments = repository.attachments(arguments.enriched_set_id)
            if arguments.projection_id:
                attachments = tuple(
                    item for item in attachments if item.projection_id == arguments.projection_id
                )
            return {
                "enriched_set_id": arguments.enriched_set_id,
                "contextual_explanations": attachments,
                **authority,
            }
        if arguments.command == "context-enriched-opportunity-set-inspect":
            enriched = repository.enriched_set(arguments.enriched_set_id)
            if enriched is None:
                raise ValueError("TOTAL_MARKET_CONTEXT_ENRICHED_SET_NOT_FOUND")
            return {
                "enriched_opportunity_set": enriched,
                "attachments": repository.attachments(enriched.enriched_set_id),
                **authority,
            }
        raise ValueError("TOTAL_MARKET_CONTEXT_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_cross_venue_allocation_command(arguments: argparse.Namespace) -> object:
    if arguments.command == "cross-venue-fixture-acceptance":
        from apex.adapters.cross_venue_allocation_acceptance import (
            run_cross_venue_fixture_acceptance,
        )

        return run_cross_venue_fixture_acceptance()
    from apex.adapters.cross_venue_allocation_postgres import (
        PostgresCrossVenueAllocationRepository,
        PostgresCrossVenueAllocationSource,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.cross_venue_allocation import MaterializeCrossVenueAllocation
    from apex.domain.cross_venue_allocation import CrossVenueAllocationPolicy

    authority = {
        "authority": "NONE",
        "capital_authority": "NONE",
        "paper_authorization_created": False,
        "reservation_created": False,
        "order_created": False,
        "live_authority": "NONE",
        "provider_requests": 0,
    }
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCrossVenueAllocationRepository(engine)
    try:
        if arguments.command == "cross-venue-policy-inspect":
            return {"policy": CrossVenueAllocationPolicy.v1(), **authority}
        if arguments.command == "cross-venue-allocation-materialize":
            result = MaterializeCrossVenueAllocation(
                repository, PostgresCrossVenueAllocationSource(engine)
            ).execute(
                decision_id=arguments.decision_id,
                enriched_set_id=arguments.enriched_set_id,
                decided_at=datetime.fromisoformat(arguments.decided_at.replace("Z", "+00:00")),
                idempotency_key=arguments.idempotency_key,
            )
            return {"allocation": result, **authority}
        if arguments.command in {
            "cross-venue-allocation-inspect",
            "cross-venue-constraint-inspect",
            "cross-venue-exposure-inspect",
            "cross-venue-lineage-inspect",
        }:
            decision = repository.decision(arguments.decision_id)
            if decision is None:
                raise ValueError("CROSS_VENUE_ALLOCATION_NOT_FOUND")
            if arguments.command == "cross-venue-constraint-inspect":
                values = repository.explanations(decision.decision_id)
                if arguments.projection_id:
                    values = tuple(
                        item for item in values if item.projection_id == arguments.projection_id
                    )
                return {"decision_id": decision.decision_id, "explanations": values, **authority}
            if arguments.command == "cross-venue-exposure-inspect":
                return {
                    "decision_id": decision.decision_id,
                    "venue_totals": decision.venue_totals,
                    "retained_cash": decision.retained_cash,
                    "modeled_downside": decision.modeled_downside,
                    "modeled_costs": decision.modeled_costs,
                    "long_lockup_capital": decision.long_lockup_capital,
                    "concentration": decision.concentration,
                    **authority,
                }
            if arguments.command == "cross-venue-lineage-inspect":
                return {
                    "decision": decision,
                    "source_ids": (
                        decision.opportunity_set_id,
                        decision.enriched_set_id,
                        decision.context_snapshot_id,
                    ),
                    "source_manifest_hash": decision.source_manifest_hash,
                    **authority,
                }
            return {
                "decision": decision,
                "lines": repository.lines(decision.decision_id),
                "explanations": repository.explanations(decision.decision_id),
                "paper_trial_proposals": repository.proposals(decision.decision_id),
                **authority,
            }
        if arguments.command == "cross-venue-paper-trial-proposal-inspect":
            proposal = repository.proposal(arguments.proposal_id)
            if proposal is None:
                raise ValueError("CROSS_VENUE_PAPER_TRIAL_PROPOSAL_NOT_FOUND")
            return {"paper_trial_proposal": proposal, **authority}
        raise ValueError("CROSS_VENUE_ALLOCATION_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_performance_attribution_command(arguments: argparse.Namespace) -> object:
    if arguments.command == "performance-attribution-fixture-acceptance":
        from apex.adapters.performance_attribution_acceptance import (
            run_performance_attribution_acceptance,
        )

        return run_performance_attribution_acceptance()
    from apex.adapters.performance_attribution_postgres import (
        PostgresPerformanceAttributionRepository,
    )
    from apex.adapters.performance_attribution_sources import (
        RetainedPerformanceAttributionSource,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.performance_attribution import (
        MaterializePerformanceAttribution,
        MaterializeStrategyHealth,
    )
    from apex.domain.performance_attribution import PaperEvidenceClass, PaperVenue

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresPerformanceAttributionRepository(engine)
    authority = {
        "authority": "NONE",
        "provider_requests": 0,
        "broker_requests": 0,
        "financial_mutations": 0,
        "paper_authorization_created": False,
        "live_authority": "NONE",
    }
    try:
        if arguments.command == "performance-attribution-materialize":
            attribution_result = MaterializePerformanceAttribution(
                repository, RetainedPerformanceAttributionSource(engine)
            ).execute(
                run_id=arguments.run_id,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                materialized_at=datetime.fromisoformat(
                    arguments.materialized_at.replace("Z", "+00:00")
                ),
                idempotency_key=arguments.idempotency_key,
                source_ids=(arguments.source_id,),
            )
            return {"materialization": attribution_result, **authority}
        if arguments.command == "performance-attribution-genuine-acceptance":
            as_of = datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00"))
            materialization = MaterializePerformanceAttribution(
                repository, RetainedPerformanceAttributionSource(engine)
            ).execute(
                run_id=arguments.run_id,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                materialized_at=as_of,
                idempotency_key=f"{arguments.idempotency_key}:attribution",
            )
            health = MaterializeStrategyHealth(repository).execute(
                run_id=arguments.run_id,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                as_of=as_of,
                idempotency_key=f"{arguments.idempotency_key}:health",
            )
            return {
                "materialization": materialization,
                "health": health,
                "genuine_workspace": True,
                "fixture_records_created": 0,
                **authority,
            }
        if arguments.command == "strategy-health-materialize":
            health_result = MaterializeStrategyHealth(repository).execute(
                run_id=arguments.run_id,
                evidence_class=PaperEvidenceClass.GENUINE_PAPER,
                as_of=datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00")),
                idempotency_key=arguments.idempotency_key,
                strategy_or_method_id=arguments.strategy_or_method_id,
            )
            return {"health_materialization": health_result, **authority}
        if arguments.command in {
            "performance-attribution-inspect",
            "performance-expected-realized-inspect",
            "performance-source-lineage-inspect",
        }:
            attribution_value = repository.attribution(arguments.attribution_id)
            if attribution_value is None:
                raise ValueError("PERFORMANCE_ATTRIBUTION_NOT_FOUND")
            if arguments.command == "performance-expected-realized-inspect":
                return {
                    "attribution_id": attribution_value.attribution_id,
                    "expected_gross_return": attribution_value.expected_gross_return,
                    "realized_gross_return": attribution_value.realized_gross_return,
                    "expected_net_return": attribution_value.expected_net_return,
                    "realized_net_return": attribution_value.realized_net_return,
                    "estimated_fees": attribution_value.estimated_fees,
                    "realized_fees": attribution_value.realized_fees,
                    "estimated_slippage": attribution_value.estimated_slippage,
                    "realized_slippage": attribution_value.realized_slippage,
                    "fill_rate": attribution_value.fill_rate,
                    "components": repository.components(attribution_value.attribution_id),
                    **authority,
                }
            if arguments.command == "performance-source-lineage-inspect":
                return {
                    "attribution_id": attribution_value.attribution_id,
                    "source_id": attribution_value.source_id,
                    "source_hash": attribution_value.source_hash,
                    "lifecycle_ids": {
                        "source_opportunity": attribution_value.source_opportunity_id,
                        "shared_projection": attribution_value.shared_projection_id,
                        "allocation_decision": attribution_value.allocation_decision_id,
                        "paper_trial_proposal": (attribution_value.paper_trial_proposal_id),
                        "authorization": attribution_value.authorization_id,
                        "order": attribution_value.order_id,
                        "reservation": attribution_value.reservation_id,
                        "fills": attribution_value.fill_ids,
                        "position": attribution_value.position_id,
                        "settlement": attribution_value.settlement_id,
                        "resolution_fact": attribution_value.resolution_fact_id,
                        "context_at_consideration": (attribution_value.context_at_consideration_id),
                        "context_at_execution": (attribution_value.context_at_execution_id),
                    },
                    "missing_stages": attribution_value.missing_stages,
                    **authority,
                }
            return {
                "attribution": attribution_value,
                "components": repository.components(attribution_value.attribution_id),
                **authority,
            }
        if arguments.command == "strategy-health-inspect":
            health_value = repository.health(arguments.health_id)
            if health_value is None:
                raise ValueError("STRATEGY_HEALTH_NOT_FOUND")
            return {"strategy_health": health_value, **authority}
        if arguments.command == "performance-venue-summary-inspect":
            venue_summary_value = repository.venue_summary(
                PaperVenue(arguments.venue), PaperEvidenceClass.GENUINE_PAPER
            )
            if venue_summary_value is None:
                raise ValueError("PERFORMANCE_VENUE_SUMMARY_NOT_FOUND")
            return {"venue_summary": venue_summary_value, **authority}
        if arguments.command == "performance-portfolio-summary-inspect":
            portfolio_summary_value = repository.portfolio_summary(PaperEvidenceClass.GENUINE_PAPER)
            if portfolio_summary_value is None:
                raise ValueError("PERFORMANCE_PORTFOLIO_SUMMARY_NOT_FOUND")
            return {"portfolio_summary": portfolio_summary_value, **authority}
        raise ValueError("PERFORMANCE_ATTRIBUTION_COMMAND_UNSUPPORTED")
    finally:
        engine.dispose()


def _run_kalshi_operating_command(arguments: argparse.Namespace) -> object:
    from apex.adapters.kalshi_operating_acceptance import (
        run_kalshi_paper_fixture_acceptance,
    )
    from apex.adapters.kalshi_operating_postgres import (
        PostgresKalshiCurrentOpportunitySource,
        PostgresKalshiOperatingRepository,
    )
    from apex.adapters.kalshi_public import (
        KalshiPublicMarketDataClient,
        KalshiPublicMarketNormalizer,
        KalshiPublicOrderbookDataClient,
    )
    from apex.adapters.operating_cycle_postgres import (
        PostgresOperatingCycleRepository,
    )
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_public_binary_complement_store,
        postgres_public_market_discovery_store,
    )
    from apex.adapters.shared_opportunity_postgres import (
        PostgresSharedOpportunityRepository,
    )
    from apex.application.discover_public_markets import (
        DiscoverPublicPredictionMarkets,
        PublicMarketScanRequest,
    )
    from apex.application.kalshi_operating import (
        ApplyKalshiPaperFill,
        AuthorizeKalshiPaperOpportunity,
        CancelKalshiPaperOrder,
        CreateKalshiPaperAccount,
        ExecuteKalshiCurrentRefresh,
        ExpireKalshiPaperOrder,
        KalshiCurrentRefreshCommand,
        PreviewKalshiPaperOrder,
        ReconcileKalshiPaperAccount,
        RetainKalshiCapability,
        RetainKalshiPaperEligibilityScan,
        SettleKalshiPaperPosition,
        SubmitKalshiPaperOrder,
        kalshi_workspace,
    )
    from apex.application.scan_public_binary_complements import (
        BinaryComplementScanRequest,
        CapturedKalshiPublicResponse,
        ScanPublicBinaryComplements,
    )
    from apex.domain.kalshi_operating import (
        KalshiCapability,
        KalshiEvidenceClass,
        KalshiPaperAuthorization,
        KalshiPaperEligibility,
        KalshiPaperEligibilityScan,
        KalshiResolutionFact,
        KalshiSide,
    )
    from apex.domain.operating_cycle import OperatingControlState

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def envelope(value: object, evidence_class: str) -> dict[str, object]:
        return {
            "result": value,
            "evidence_class": evidence_class,
            "warning": "KALSHI PAPER — SIMULATED CAPITAL ONLY",
            "provider_writes": 0,
            "genuine_financial_mutation": False,
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
        }

    if arguments.command == "kalshi-paper-fixture-acceptance":
        return envelope(
            run_kalshi_paper_fixture_acceptance(),
            "FIXTURE_ISOLATED_ACCEPTANCE",
        )
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresKalshiOperatingRepository(engine)
    try:
        if arguments.command == "kalshi-operating-capability-inspect":
            capability = KalshiCapability.inspect(
                capability_id=arguments.capability_id,
                inspected_at=parsed(arguments.inspected_at),
                public_discovery=True,
                public_orderbook=True,
                public_rules=True,
            )
            return envelope(
                RetainKalshiCapability(repository).execute(
                    capability=capability,
                    idempotency_key=arguments.idempotency_key,
                ),
                "LOCAL_CAPABILITY_CONTRACT",
            )
        if arguments.command in (
            "kalshi-operating-refresh",
            "kalshi-operating-refresh-replay",
        ):
            retained_capability = repository.latest_capability()
            if retained_capability is None or not retained_capability.public_refresh_available:
                raise ValueError("KALSHI_PUBLIC_REFRESH_CAPABILITY_NOT_RETAINED")
            request_count = [0]
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
                    request_count[0] += 1
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
                    request_count[0] += 1
                    return orderbook_client.fetch_market(
                        market_ticker=market_ticker,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_event(
                    self, *, event_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    request_count[0] += 1
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
                    request_count[0] += 1
                    return orderbook_client.fetch_orderbook(
                        market_ticker=market_ticker,
                        maximum_depth=maximum_depth,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_series(
                    self, *, series_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    request_count[0] += 1
                    return orderbook_client.fetch_series(
                        series_ticker=series_ticker,
                        timeout_seconds=timeout_seconds,
                    )

                def fetch_series_fee_changes(
                    self, *, series_ticker: str, timeout_seconds: int
                ) -> CapturedKalshiPublicResponse:
                    request_count[0] += 1
                    return orderbook_client.fetch_series_fee_changes(
                        series_ticker=series_ticker,
                        timeout_seconds=timeout_seconds,
                    )

            market_request = PublicMarketScanRequest(
                run_key=arguments.run_key,
                page_size=arguments.market_limit,
                maximum_pages=arguments.max_pages,
                timeout_seconds=arguments.timeout_seconds,
            )
            refresh_result = ExecuteKalshiCurrentRefresh(
                repository=repository,
                discovery=DiscoverPublicPredictionMarkets(
                    client=CountingMarketClient(),
                    normalizer=KalshiPublicMarketNormalizer(),
                    store=postgres_public_market_discovery_store(engine),
                    clock=lambda: datetime.now(tz=UTC),
                ),
                complements=ScanPublicBinaryComplements(
                    client=CountingOrderbookClient(),
                    store=postgres_public_binary_complement_store(engine),
                    clock=lambda: datetime.now(tz=UTC),
                ),
                opportunity_source=PostgresKalshiCurrentOpportunitySource(engine),
                provider_request_count=lambda: request_count[0],
                clock=lambda: datetime.now(tz=UTC),
            ).execute(
                KalshiCurrentRefreshCommand(
                    refresh_id=arguments.refresh_id,
                    as_of=parsed(arguments.as_of),
                    evidence_class=KalshiEvidenceClass.GENUINE_PUBLIC,
                    market_request=market_request,
                    complement_request=BinaryComplementScanRequest(
                        market_discovery_run_id=market_request.ingestion_run_id,
                        market_limit=arguments.market_limit,
                        maximum_depth=arguments.depth,
                        maximum_bundle_quantity=Decimal(10),
                        timeout_seconds=arguments.timeout_seconds,
                    ),
                    idempotency_key=arguments.idempotency_key,
                )
            )
            return envelope(
                {
                    "refresh": refresh_result.value,
                    "opportunities": repository.opportunities_for_refresh(
                        refresh_result.value.refresh_id
                    ),
                    "idempotent_replay": refresh_result.idempotent_replay,
                    "provider_requests_performed": refresh_result.provider_requests,
                },
                "GENUINE_PUBLIC",
            )
        if arguments.command == "kalshi-operating-refresh-inspect":
            refresh = repository.refresh(arguments.refresh_id)
            if refresh is None:
                raise ValueError("KALSHI_REFRESH_NOT_FOUND")
            return envelope(
                {
                    "refresh": refresh,
                    "opportunities": repository.opportunities_for_refresh(refresh.refresh_id),
                },
                refresh.evidence_class.value,
            )
        if arguments.command == "kalshi-operating-opportunity-inspect":
            opportunity = next(
                (
                    item
                    for item in repository.opportunities_for_refresh(arguments.refresh_id)
                    if item.opportunity_id == arguments.opportunity_id
                ),
                None,
            )
            if opportunity is None:
                raise ValueError("KALSHI_CURRENT_OPPORTUNITY_NOT_FOUND")
            return envelope(opportunity, opportunity.evidence_class.value)
        if arguments.command in {
            "kalshi-operating-market-inspect",
            "kalshi-operating-rule-inspect",
            "kalshi-operating-orderbook-inspect",
        }:
            if arguments.command == "kalshi-operating-orderbook-inspect":
                retained_orderbook = repository.retained_orderbook(arguments.orderbook_snapshot_id)
                if retained_orderbook is None:
                    raise ValueError("KALSHI_ORDERBOOK_NOT_FOUND")
                book, levels = retained_orderbook
                return envelope(
                    {"orderbook": book, "levels": levels},
                    "RETAINED_PUBLIC_EVIDENCE",
                )
            market = repository.retained_market(arguments.market_snapshot_id)
            if market is None:
                raise ValueError("KALSHI_MARKET_NOT_FOUND")
            value = (
                {
                    "snapshot_id": market["snapshot_id"],
                    "market_ticker": market["provider_market_ticker"],
                    "event_ticker": market["event_ticker"],
                    "rules_primary": market["rules_primary"],
                    "rules_secondary": market["rules_secondary"],
                    "rule_content_hash": market["rule_content_hash"],
                }
                if arguments.command == "kalshi-operating-rule-inspect"
                else market
            )
            return envelope(value, "RETAINED_PUBLIC_EVIDENCE")
        if arguments.command == "kalshi-paper-account-create":
            return envelope(
                CreateKalshiPaperAccount(repository).execute(
                    account_id=arguments.account_id,
                    opening_cash=arguments.opening_cash,
                    created_at=parsed(arguments.created_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                "APEX_INTERNAL_PAPER",
            )
        if arguments.command == "kalshi-paper-account-inspect":
            return envelope(
                kalshi_workspace(
                    repository=repository,
                    account_id=arguments.account_id,
                    capability_id=(
                        None
                        if repository.latest_capability() is None
                        else repository.latest_capability().capability_id  # type: ignore[union-attr]
                    ),
                    refresh_id=(
                        None
                        if repository.latest_refresh() is None
                        else repository.latest_refresh().refresh_id  # type: ignore[union-attr]
                    ),
                ),
                "APEX_INTERNAL_PAPER",
            )
        if arguments.command == "kalshi-paper-eligibility-scan":
            shared = PostgresSharedOpportunityRepository(engine)
            operating = PostgresOperatingCycleRepository(engine)
            opportunity_set = shared.opportunity_set(arguments.shared_set_id)
            plan = operating.plan(arguments.operating_plan_id)
            if opportunity_set is None or plan is None:
                raise ValueError("KALSHI_PAPER_SCAN_INPUT_NOT_FOUND")
            controls = operating.control_events(arguments.operating_plan_id)
            enabled = plan.enabled and (
                not controls or controls[-1].state is OperatingControlState.ENABLED
            )
            shared_decisions = {
                item.projection_id: item
                for item in shared.eligibility_decisions(arguments.shared_set_id)
            }
            shared_by_evaluation = {
                projection.source_record_id: shared_decisions.get(projection.projection_id)
                for projection_id in opportunity_set.projection_ids
                if (projection := shared.projection(projection_id)) is not None
                and projection.venue.value == "KALSHI"
            }
            state = None if arguments.account_id is None else repository.state(arguments.account_id)
            decisions = tuple(
                KalshiPaperEligibility.evaluate(
                    decision_id=f"{arguments.scan_id}-{item.opportunity_id}",
                    opportunity=item,
                    account=None if state is None else state.account,
                    available_cash=0 if state is None else state.available_cash,
                    shared_eligible=bool(
                        (decision := shared_by_evaluation.get(item.evaluation_id)) is not None
                        and decision.state.value == "ELIGIBLE"
                    ),
                    operating_cycle_enabled=enabled,
                    evaluated_at=parsed(arguments.evaluated_at),
                )
                for item in repository.opportunities_for_refresh(arguments.refresh_id)
            )
            scan = KalshiPaperEligibilityScan.create(
                scan_id=arguments.scan_id,
                refresh_id=arguments.refresh_id,
                decisions=decisions,
                evaluated_at=parsed(arguments.evaluated_at),
            )
            scan_result = RetainKalshiPaperEligibilityScan(repository).execute(
                scan=scan,
                decisions=decisions,
                idempotency_key=arguments.idempotency_key,
            )
            return envelope(
                {
                    "scan": scan_result.value,
                    "decisions": decisions,
                    "idempotent_replay": scan_result.idempotent_replay,
                    "authorizations_created": 0,
                    "orders_created": 0,
                },
                "GENUINE_RETAINED_STATE",
            )
        if arguments.command == "kalshi-paper-authorization-create":
            opportunity = next(
                (
                    item
                    for item in repository.opportunities_for_refresh(arguments.refresh_id)
                    if item.opportunity_id == arguments.opportunity_id
                ),
                None,
            )
            eligibility = next(
                (
                    item
                    for item in repository.scan_decisions(arguments.scan_id)
                    if item.decision_id == arguments.decision_id
                ),
                None,
            )
            if opportunity is None or eligibility is None:
                raise ValueError("KALSHI_PAPER_AUTHORIZATION_INPUT_NOT_FOUND")
            authorization = KalshiPaperAuthorization.create(
                authorization_id=arguments.authorization_id,
                account_id=arguments.account_id,
                opportunity=opportunity,
                eligibility=eligibility,
                maximum_contracts=arguments.maximum_contracts,
                maximum_dollar_risk=arguments.maximum_dollar_risk,
                expires_at=parsed(arguments.expires_at),
                actor=arguments.actor,
                reason=arguments.reason,
                idempotency_key=arguments.idempotency_key,
                created_at=parsed(arguments.created_at),
            )
            return envelope(
                AuthorizeKalshiPaperOpportunity(repository).execute(authorization=authorization),
                "APEX_INTERNAL_PAPER",
            )
        if arguments.command == "kalshi-paper-preview":
            opportunity = next(
                (
                    item
                    for item in repository.opportunities_for_refresh(arguments.refresh_id)
                    if item.opportunity_id == arguments.opportunity_id
                ),
                None,
            )
            if opportunity is None:
                raise ValueError("KALSHI_CURRENT_OPPORTUNITY_NOT_FOUND")
            evidence_observed_at = parsed(arguments.evidence_observed_at)
            if arguments.evidence_id not in opportunity.supporting_evidence_ids:
                raise ValueError("KALSHI_PAPER_PREVIEW_EVIDENCE_NOT_BOUND_TO_OPPORTUNITY")
            retained_liquidity = repository.retained_orderbook_liquidity(
                evidence_id=arguments.evidence_id,
                market_ticker=opportunity.market_ticker,
                side=opportunity.side,
                observed_at=evidence_observed_at,
                maximum_price=Decimal(arguments.limit_price),
            )
            if retained_liquidity != arguments.available_liquidity:
                raise ValueError("KALSHI_PAPER_PREVIEW_LIQUIDITY_MISMATCH")
            return envelope(
                PreviewKalshiPaperOrder(repository).execute(
                    account_id=arguments.account_id,
                    opportunity=opportunity,
                    preview_id=arguments.preview_id,
                    authorization_id=arguments.authorization_id,
                    limit_price=arguments.limit_price,
                    contracts=arguments.contracts,
                    available_liquidity=arguments.available_liquidity,
                    evidence_id=arguments.evidence_id,
                    evidence_observed_at=evidence_observed_at,
                    created_at=parsed(arguments.created_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                opportunity.evidence_class.value,
            )
        if arguments.command == "kalshi-paper-submit":
            return envelope(
                SubmitKalshiPaperOrder(repository).execute(
                    account_id=arguments.account_id,
                    order_id=arguments.order_id,
                    preview_id=arguments.preview_id,
                    submitted_at=parsed(arguments.submitted_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                "APEX_INTERNAL_PAPER",
            )
        if arguments.command == "kalshi-paper-apply-fill":
            state = repository.state(arguments.account_id)
            if state is None:
                raise ValueError("KALSHI_PAPER_ACCOUNT_NOT_FOUND")
            order = next(
                (item for item in state.orders if item.order_id == arguments.order_id),
                None,
            )
            if order is None:
                raise ValueError("KALSHI_PAPER_ORDER_NOT_FOUND")
            evidence_observed_at = parsed(arguments.evidence_observed_at)
            retained_liquidity = repository.retained_orderbook_liquidity(
                evidence_id=arguments.evidence_id,
                market_ticker=order.market_ticker,
                side=order.side,
                observed_at=evidence_observed_at,
                maximum_price=order.limit_price,
                exact_price=Decimal(arguments.price),
            )
            if retained_liquidity < arguments.contracts:
                raise ValueError("KALSHI_PAPER_FILL_VISIBLE_LIQUIDITY_INSUFFICIENT")
            return envelope(
                ApplyKalshiPaperFill(repository).execute(
                    account_id=arguments.account_id,
                    order_id=arguments.order_id,
                    fill_id=arguments.fill_id,
                    contracts=arguments.contracts,
                    price=arguments.price,
                    evidence_id=arguments.evidence_id,
                    evidence_observed_at=evidence_observed_at,
                    ingested_at=parsed(arguments.ingested_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                "RETAINED_LATER_MARKET_EVIDENCE",
            )
        if arguments.command == "kalshi-paper-cancel":
            return envelope(
                CancelKalshiPaperOrder(repository).execute(
                    account_id=arguments.account_id,
                    order_id=arguments.order_id,
                    canceled_at=parsed(arguments.canceled_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                "APEX_INTERNAL_PAPER",
            )
        if arguments.command == "kalshi-paper-expire":
            return envelope(
                ExpireKalshiPaperOrder(repository).execute(
                    account_id=arguments.account_id,
                    order_id=arguments.order_id,
                    expired_at=parsed(arguments.expired_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                "APEX_INTERNAL_PAPER",
            )
        if arguments.command == "kalshi-paper-settle":
            if arguments.evidence_class == KalshiEvidenceClass.GENUINE_PUBLIC.value:
                raise ValueError("KALSHI_GENUINE_RESOLUTION_FACT_IMPORT_UNAVAILABLE")
            fact = KalshiResolutionFact.create(
                resolution_fact_id=arguments.resolution_fact_id,
                market_ticker=arguments.market_ticker,
                event_ticker=arguments.event_ticker,
                resolution=KalshiSide(arguments.resolution),
                resolution_source=arguments.resolution_source,
                rule_hash=arguments.rule_hash,
                settled_at=parsed(arguments.settled_at),
                retained_at=parsed(arguments.retained_at),
                evidence_class=KalshiEvidenceClass(arguments.evidence_class),
            )
            return envelope(
                SettleKalshiPaperPosition(repository).execute(
                    account_id=arguments.account_id,
                    position_id=arguments.position_id,
                    fact=fact,
                    settlement_id=arguments.settlement_id,
                    idempotency_key=arguments.idempotency_key,
                ),
                arguments.evidence_class,
            )
        if arguments.command == "kalshi-paper-reconcile":
            return envelope(
                ReconcileKalshiPaperAccount(repository).execute(
                    account_id=arguments.account_id,
                    reconciliation_id=arguments.reconciliation_id,
                    reconciled_at=parsed(arguments.reconciled_at),
                    idempotency_key=arguments.idempotency_key,
                ),
                "APEX_INTERNAL_PAPER",
            )
        state = repository.state(arguments.account_id)
        if state is None:
            raise ValueError("KALSHI_PAPER_ACCOUNT_NOT_FOUND")
        identity_name, collection_name = {
            "kalshi-paper-authorization-inspect": (
                "authorization_id",
                "authorizations",
            ),
            "kalshi-paper-order-inspect": ("order_id", "orders"),
            "kalshi-paper-fill-inspect": ("fill_id", "fills"),
            "kalshi-paper-position-inspect": ("position_id", "positions"),
            "kalshi-paper-reconciliation-inspect": (
                "reconciliation_id",
                "reconciliations",
            ),
        }.get(arguments.command, ("", ""))
        if not identity_name:
            raise ValueError("KALSHI_OPERATING_COMMAND_UNSUPPORTED")
        identity = getattr(arguments, identity_name)
        entity = next(
            (
                item
                for item in getattr(state, collection_name)
                if getattr(item, identity_name) == identity
            ),
            None,
        )
        if entity is None:
            raise ValueError("KALSHI_PAPER_EXACT_ENTITY_NOT_FOUND")
        return envelope(entity, "APEX_INTERNAL_PAPER")
    finally:
        engine.dispose()


def _run_crypto_paper_command(arguments: argparse.Namespace) -> object:
    from apex.adapters.crypto_paper_acceptance import (
        run_crypto_paper_fixture_acceptance,
    )
    from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
    from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
    from apex.adapters.operating_cycle_postgres import PostgresOperatingCycleRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.shared_opportunity_postgres import (
        PostgresSharedOpportunityRepository,
    )
    from apex.application.crypto_paper import (
        ApplyCryptoPaperMarketEvidence,
        AuthorizeCryptoPaperOpportunity,
        CancelCryptoPaperOrder,
        ConstructCryptoPaperOrderIntent,
        CorrectCryptoPaperCloseAccounting,
        CreateCryptoPaperAccount,
        ExpireCryptoPaperOrder,
        InspectCryptoPaperOperatingSeam,
        PreviewCryptoPaperOrder,
        ReconcileCryptoPaperAccount,
        RegisterCryptoPaperProviderContract,
        RetainCryptoPaperCapability,
        RetainCryptoPaperEligibilityScan,
        SubmitCryptoPaperOrder,
        paper_workspace,
    )
    from apex.domain.crypto_paper import (
        CryptoPaperAuthorization,
        CryptoPaperEligibilityScan,
        PaperOrderState,
        PaperOrderType,
        PaperPricingSource,
        PaperSide,
        RetainedPaperPriceEvidence,
        build_provider_contract,
        evaluate_paper_eligibility,
        inspect_paper_capability,
        ledger_account_balances,
        ledger_contract_discrepancies,
    )
    from apex.domain.operating_cycle import OperatingControlState
    from apex.domain.shared_opportunity import SharedVenue

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def output(value: object, *, evidence_class: str) -> dict[str, object]:
        return {
            "result": value,
            "evidence_class": evidence_class,
            "paper_authority": "PAPER_ONLY",
            "live_authority": "NONE",
            "provider_requests_performed": 0,
            "genuine_financial_mutation": False,
            "cash_and_reservation_effects": "APEX_INTERNAL_PAPER_ONLY",
        }

    if arguments.command == "crypto-paper-fixture-acceptance":
        return output(
            run_crypto_paper_fixture_acceptance(),
            evidence_class="FIXTURE_ISOLATED_ACCEPTANCE",
        )
    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCryptoPaperRepository(engine)
    try:
        if arguments.command == "crypto-paper-contract-register":
            universe_repository = PostgresCryptoUniverseRepository(engine)
            retained_products = universe_repository.products(arguments.capture_id)
            product = next(
                (item for item in retained_products if item.product_id == arguments.product_id),
                None,
            )
            if product is None:
                raise ValueError("CRYPTO_PAPER_EXACT_PRODUCT_NOT_FOUND")
            registered_contract = build_provider_contract(
                contract_id=arguments.contract_id,
                product=product,
                source_capture_id=arguments.capture_id,
            )
            contract_result = RegisterCryptoPaperProviderContract(repository).execute(
                contract=registered_contract,
                idempotency_key=arguments.idempotency_key,
            )
            return output(contract_result, evidence_class=product.evidence_class.value)
        if arguments.command == "crypto-paper-capability-inspect":
            retained_contract = repository.contract(arguments.contract_id)
            if retained_contract is None:
                raise ValueError("CRYPTO_PAPER_CONTRACT_NOT_FOUND")
            capability = inspect_paper_capability(
                contract=retained_contract,
                inspected_at=parsed(arguments.inspected_at),
            )
            if capability.capability_id != arguments.capability_id:
                raise ValueError("CRYPTO_PAPER_CAPABILITY_ID_MISMATCH")
            capability_result = RetainCryptoPaperCapability(repository).execute(
                capability=capability,
                idempotency_key=arguments.idempotency_key,
            )
            return output(
                capability_result,
                evidence_class="RETAINED_PRODUCT_CONTRACT",
            )
        if arguments.command == "crypto-paper-account-create":
            account_result = CreateCryptoPaperAccount(repository).execute(
                account_id=arguments.account_id,
                opening_cash=arguments.opening_cash,
                created_at=parsed(arguments.created_at),
                idempotency_key=arguments.idempotency_key,
            )
            return output(account_result, evidence_class="APEX_INTERNAL_PAPER")
        if arguments.command == "crypto-paper-account-inspect":
            return output(
                paper_workspace(repository.state(arguments.account_id)),
                evidence_class="APEX_INTERNAL_PAPER",
            )
        if arguments.command == "crypto-paper-open-orders":
            state = repository.state(arguments.account_id)
            if state is None:
                raise ValueError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
            return output(
                tuple(
                    item
                    for item in state.orders
                    if item.state in {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}
                ),
                evidence_class="APEX_INTERNAL_PAPER",
            )
        if arguments.command == "crypto-paper-operating-seam-inspect":
            return output(
                InspectCryptoPaperOperatingSeam(repository).execute(
                    account_id=arguments.account_id
                ),
                evidence_class="APEX_INTERNAL_PAPER",
            )
        if arguments.command == "crypto-paper-genuine-eligibility-scan":
            shared_repository = PostgresSharedOpportunityRepository(engine)
            universe_repository = PostgresCryptoUniverseRepository(engine)
            operating_repository = PostgresOperatingCycleRepository(engine)
            opportunity_set = shared_repository.opportunity_set(arguments.opportunity_set_id)
            universe = universe_repository.universe_snapshot(arguments.universe_id)
            operating_plan = operating_repository.plan(arguments.operating_plan_id)
            if opportunity_set is None:
                raise ValueError("CRYPTO_PAPER_SHARED_SET_NOT_FOUND")
            if universe is None:
                raise ValueError("CRYPTO_PAPER_UNIVERSE_NOT_FOUND")
            if operating_plan is None:
                raise ValueError("CRYPTO_PAPER_OPERATING_PLAN_NOT_FOUND")
            controls = operating_repository.control_events(arguments.operating_plan_id)
            operating_enabled = operating_plan.enabled and (
                not controls or controls[-1].state is OperatingControlState.ENABLED
            )
            shared_decisions = {
                item.projection_id: item
                for item in shared_repository.eligibility_decisions(
                    opportunity_set.opportunity_set_id
                )
            }
            products_by_id = {
                item.product_id: item for item in universe_repository.products(arguments.capture_id)
            }
            decisions = []
            for projection_id in opportunity_set.projection_ids:
                projection = shared_repository.projection(projection_id)
                if projection is None or projection.venue is not SharedVenue.CRYPTO:
                    continue
                shared_decision = shared_decisions.get(projection.projection_id)
                if shared_decision is None:
                    raise ValueError("CRYPTO_PAPER_SHARED_DECISION_NOT_FOUND")
                product = products_by_id.get(projection.product_or_market_id)
                scan_contract = (
                    None
                    if product is None
                    else build_provider_contract(
                        contract_id=(
                            f"{arguments.scan_id}-{projection.product_or_market_id}-contract"
                        ),
                        product=product,
                        source_capture_id=arguments.capture_id,
                    )
                )
                decisions.append(
                    evaluate_paper_eligibility(
                        decision_id=f"{arguments.scan_id}-{projection.projection_id}",
                        opportunity_set_id=opportunity_set.opportunity_set_id,
                        projection=projection,
                        shared_decision=shared_decision,
                        universe=universe,
                        contract=scan_contract,
                        account=None,
                        account_state=None,
                        operating_cycle_enabled=operating_enabled,
                        evaluated_at=parsed(arguments.evaluated_at),
                    )
                )
            scan = CryptoPaperEligibilityScan.create(
                scan_id=arguments.scan_id,
                opportunity_set_id=opportunity_set.opportunity_set_id,
                decisions=tuple(decisions),
                evaluated_at=parsed(arguments.evaluated_at),
            )
            scan_result = RetainCryptoPaperEligibilityScan(repository).execute(
                scan=scan,
                decisions=tuple(decisions),
                idempotency_key=arguments.idempotency_key,
            )
            return output(
                {
                    "scan": scan_result.value,
                    "decisions": tuple(decisions),
                    "crypto_source_records": len(decisions),
                    "eligible_records": scan.eligible_count,
                    "authorizations_created": 0,
                    "orders_created": 0,
                    "idempotent_replay": scan_result.idempotent_replay,
                },
                evidence_class="GENUINE_RETAINED_STATE",
            )
        if arguments.command == "crypto-paper-authorization-create":
            state = repository.state(arguments.account_id)
            if state is None:
                raise ValueError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
            retained_decisions = repository.scan_decisions(arguments.scan_id)
            eligibility = next(
                (item for item in retained_decisions if item.decision_id == arguments.decision_id),
                None,
            )
            projection = PostgresSharedOpportunityRepository(engine).projection(
                arguments.projection_id
            )
            if eligibility is None:
                raise ValueError("CRYPTO_PAPER_ELIGIBILITY_NOT_FOUND")
            if projection is None:
                raise ValueError("CRYPTO_PAPER_PROJECTION_NOT_FOUND")
            authorization = CryptoPaperAuthorization.create(
                authorization_id=arguments.authorization_id,
                account_id=arguments.account_id,
                eligibility=eligibility,
                projection=projection,
                maximum_quote_notional=arguments.maximum_quote_notional,
                maximum_base_size=arguments.maximum_base_size,
                allowed_side=PaperSide(arguments.allowed_side),
                allowed_order_type=PaperOrderType(arguments.allowed_order_type),
                expires_at=parsed(arguments.expires_at),
                actor=arguments.actor,
                reason=arguments.reason,
                idempotency_key=arguments.idempotency_key,
                created_at=parsed(arguments.created_at),
            )
            return output(
                AuthorizeCryptoPaperOpportunity(repository).execute(
                    account_id=arguments.account_id,
                    authorization=authorization,
                ),
                evidence_class="RETAINED_SHARED_OPPORTUNITY",
            )
        if arguments.command == "crypto-paper-authorization-inspect":
            state = repository.state(arguments.account_id)
            if state is None:
                raise ValueError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
            value = next(
                (
                    item
                    for item in state.authorizations
                    if item.authorization_id == arguments.authorization_id
                ),
                None,
            )
            if value is None:
                raise ValueError("CRYPTO_PAPER_AUTHORIZATION_NOT_FOUND")
            return output(value, evidence_class="APEX_INTERNAL_PAPER")
        if arguments.command == "crypto-paper-intent-construct":
            intent_result = ConstructCryptoPaperOrderIntent(repository).execute(
                account_id=arguments.account_id,
                intent_id=arguments.intent_id,
                authorization_id=arguments.authorization_id,
                contract_id=arguments.contract_id,
                side=PaperSide(arguments.side),
                order_type=PaperOrderType(arguments.order_type),
                quote_size=arguments.quote_size,
                base_size=arguments.base_size,
                limit_price=arguments.limit_price,
                created_at=parsed(arguments.created_at),
                idempotency_key=arguments.idempotency_key,
            )
            return output(intent_result, evidence_class="APEX_INTERNAL_PAPER")

        def evidence() -> RetainedPaperPriceEvidence:
            return RetainedPaperPriceEvidence.create(
                evidence_id=arguments.evidence_id,
                product_id=arguments.product_id,
                observed_at=parsed(arguments.observed_at),
                ingested_at=parsed(arguments.ingested_at),
                price=arguments.price,
                available_base_quantity=arguments.available_base_quantity,
                source=PaperPricingSource(arguments.pricing_source),
                evidence_class=arguments.evidence_class,
            )

        if arguments.command == "crypto-paper-preview":
            preview_result = PreviewCryptoPaperOrder(repository).execute(
                account_id=arguments.account_id,
                preview_id=arguments.preview_id,
                intent_id=arguments.intent_id,
                evidence=evidence(),
                fee_bps=arguments.fee_bps,
                spread_bps=arguments.spread_bps,
                slippage_bps=arguments.slippage_bps,
                created_at=parsed(arguments.created_at),
                idempotency_key=arguments.idempotency_key,
            )
            return output(preview_result, evidence_class=arguments.evidence_class)
        if arguments.command == "crypto-paper-submit":
            submission_result = SubmitCryptoPaperOrder(repository).execute(
                account_id=arguments.account_id,
                order_id=arguments.order_id,
                reservation_id=arguments.reservation_id,
                preview_id=arguments.preview_id,
                submitted_at=parsed(arguments.submitted_at),
                valid_until=parsed(arguments.valid_until),
                actor=arguments.actor,
                idempotency_key=arguments.idempotency_key,
            )
            return output(submission_result, evidence_class="APEX_INTERNAL_PAPER")
        if arguments.command == "crypto-paper-apply-evidence":
            fill_result = ApplyCryptoPaperMarketEvidence(repository).execute(
                account_id=arguments.account_id,
                order_id=arguments.order_id,
                fill_id=arguments.fill_id,
                evidence=evidence(),
                fee_bps=arguments.fee_bps,
                slippage_bps=arguments.slippage_bps,
                actor=arguments.actor,
                idempotency_key=arguments.idempotency_key,
            )
            return output(fill_result, evidence_class=arguments.evidence_class)
        if arguments.command == "crypto-paper-cancel":
            cancel_result = CancelCryptoPaperOrder(repository).execute(
                account_id=arguments.account_id,
                order_id=arguments.order_id,
                canceled_at=parsed(arguments.canceled_at),
                actor=arguments.actor,
                reason=arguments.reason,
                idempotency_key=arguments.idempotency_key,
            )
            return output(cancel_result, evidence_class="APEX_INTERNAL_PAPER")
        if arguments.command == "crypto-paper-expire":
            expiry_result = ExpireCryptoPaperOrder(repository).execute(
                account_id=arguments.account_id,
                order_id=arguments.order_id,
                expired_at=parsed(arguments.expired_at),
                actor=arguments.actor,
                idempotency_key=arguments.idempotency_key,
            )
            return output(expiry_result, evidence_class="APEX_INTERNAL_PAPER")
        if arguments.command == "crypto-paper-ledger-inspect":
            ledger_state = repository.state(arguments.account_id)
            if ledger_state is None:
                raise ValueError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
            return output(
                {
                    "account_id": ledger_state.account.account_id,
                    "state_hash": ledger_state.canonical_hash,
                    "ledger_balances": ledger_account_balances(ledger_state.journals),
                    "journal_ids": tuple(item.journal_id for item in ledger_state.journals),
                    "account_cash": ledger_state.account_state.cash,
                    "account_realized_paper_pnl": ledger_state.account_state.realized_paper_pnl,
                    "account_modeled_fees": ledger_state.account_state.modeled_fees,
                    "ledger_contract_discrepancies": ledger_contract_discrepancies(
                        account_state=ledger_state.account_state,
                        fills=ledger_state.fills,
                        positions=ledger_state.positions,
                        journals=ledger_state.journals,
                    ),
                },
                evidence_class="APEX_INTERNAL_PAPER",
            )
        if arguments.command == "crypto-paper-close-accounting-correct":
            corrector = CorrectCryptoPaperCloseAccounting(repository)
            if not arguments.apply:
                return output(
                    {
                        "dry_run": True,
                        "correction": corrector.plan(
                            account_id=arguments.account_id,
                            sell_fill_id=arguments.sell_fill_id,
                            expected_source_journal_hash=arguments.expected_source_journal_hash,
                            expected_restatement=arguments.expected_restatement,
                        ),
                    },
                    evidence_class="APEX_INTERNAL_PAPER",
                )
            return output(
                corrector.execute(
                    account_id=arguments.account_id,
                    sell_fill_id=arguments.sell_fill_id,
                    expected_source_journal_hash=arguments.expected_source_journal_hash,
                    expected_restatement=arguments.expected_restatement,
                    idempotency_key=arguments.idempotency_key,
                ),
                evidence_class="APEX_INTERNAL_PAPER",
            )
        if arguments.command == "crypto-paper-reconcile":
            reconciliation_result = ReconcileCryptoPaperAccount(repository).execute(
                account_id=arguments.account_id,
                reconciliation_id=arguments.reconciliation_id,
                evaluated_at=parsed(arguments.evaluated_at),
                order_id=arguments.order_id,
                idempotency_key=arguments.idempotency_key,
            )
            return output(
                reconciliation_result,
                evidence_class="APEX_INTERNAL_PAPER",
            )
        state = repository.state(arguments.account_id)
        if state is None:
            raise ValueError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
        entity: object | None
        if arguments.command == "crypto-paper-order-inspect":
            entity = next(
                (item for item in state.orders if item.order_id == arguments.order_id),
                None,
            )
        elif arguments.command == "crypto-paper-fill-inspect":
            entity = next(
                (item for item in state.fills if item.fill_id == arguments.fill_id),
                None,
            )
        elif arguments.command == "crypto-paper-position-inspect":
            entity = next(
                (item for item in state.positions if item.position_id == arguments.position_id),
                None,
            )
        elif arguments.command == "crypto-paper-reconciliation-inspect":
            entity = next(
                (
                    item
                    for item in state.reconciliations
                    if item.reconciliation_id == arguments.reconciliation_id
                ),
                None,
            )
        else:
            raise ValueError("CRYPTO_PAPER_COMMAND_UNSUPPORTED")
        if entity is None:
            raise ValueError("CRYPTO_PAPER_EXACT_ENTITY_NOT_FOUND")
        return output(entity, evidence_class="APEX_INTERNAL_PAPER")
    finally:
        engine.dispose()


def main() -> None:
    arguments = parser().parse_args()
    if arguments.command == "demo-core":
        report_bytes = demo_core(arguments.fixture, arguments.capital, arguments.seed)
        sys.stdout.buffer.write(report_bytes)
        print(report_sha256(report_bytes), file=sys.stderr)
        return
    try:
        if arguments.command.startswith(("performance-", "strategy-health-")):
            _print_autopilot(_run_performance_attribution_command(arguments))
            return
        if arguments.command.startswith("cross-venue-"):
            _print_autopilot(_run_cross_venue_allocation_command(arguments))
            return
        if arguments.command.startswith(("kalshi-operating-", "kalshi-paper-")):
            _print_autopilot(_run_kalshi_operating_command(arguments))
            return
        if arguments.command.startswith("crypto-paper-"):
            _print_autopilot(_run_crypto_paper_command(arguments))
            return
        if arguments.command.startswith("operating-"):
            _print_autopilot(_run_operating_cycle_command(arguments))
            return
        if arguments.command.startswith("live-observer-"):
            _print_autopilot(_run_operating_cycle_command(arguments))
            return
        if arguments.command.startswith("shared-"):
            _print_autopilot(_run_shared_opportunity_command(arguments))
            return
        if arguments.command.startswith("counterfactual-"):
            _print_autopilot(_run_counterfactual_ledger_command(arguments))
            return
        if arguments.command.startswith("followup-"):
            _print_autopilot(_run_counterfactual_followup_command(arguments))
            return
        if arguments.command.startswith("external-strategy-"):
            _print_autopilot(_run_external_strategy_research_command(arguments))
            return
        if arguments.command.startswith("internal-strategy-"):
            _print_autopilot(_run_internal_strategy_history_command(arguments))
            return
        if arguments.command.startswith("evidence-gap-"):
            _print_autopilot(_run_evidence_gap_command(arguments))
            return
        if arguments.command == "research-agenda-recalibrate" or arguments.command.startswith(
            ("research-calibration-", "research-evidence-grade-", "follow-up-priority-")
        ):
            _print_autopilot(_run_research_calibration_command(arguments))
            return
        if arguments.command == "follow-up-requalify" or arguments.command.startswith(
            (
                "calibration-v2-",
                "calibration-strata-",
                "calibration-stability-",
                "evidence-grade-v2-",
            )
        ):
            _print_autopilot(_run_research_calibration_v2_command(arguments))
            return
        if arguments.command.startswith(("research-replication-", "replication-")):
            _print_autopilot(_run_research_replication_command(arguments))
            return
        if arguments.command.startswith(
            (
                "research-agenda-",
                "hypothesis-proposals-",
                "agenda-run-",
                "research-knowledge-",
                "research-contradictions-",
                "follow-up-proposals-",
            )
        ):
            _print_autopilot(_run_cross_market_research_command(arguments))
            return
        if (
            arguments.command.startswith("context-")
            or arguments.command.startswith("contextual-")
            or arguments.command == "opportunity-context-attach"
        ):
            _print_autopilot(_run_total_market_context_command(arguments))
            return
        if arguments.command == "autopilot-strategies":
            _print_autopilot(autopilot_strategies())
            return
        if arguments.command == "autopilot-status":
            _print_autopilot(autopilot_status(arguments.campaign_id))
            return
        if arguments.command == "autopilot-cycle":
            _print_autopilot(
                _autopilot_cycle_summary(
                    autopilot_cycle(
                        campaign_id=arguments.campaign_id,
                        mode=arguments.mode,
                        idempotency_key=arguments.idempotency_key,
                        maximum_strategies=arguments.maximum_strategies,
                        maximum_proposals=arguments.maximum_proposals,
                        deadline_seconds=arguments.deadline_seconds,
                        operator_note=arguments.operator_note,
                    )
                )
            )
            return
        if arguments.command in ("autopilot-pause", "autopilot-resume", "autopilot-kill"):
            _print_autopilot(
                autopilot_control(
                    campaign_id=arguments.campaign_id,
                    action={
                        "autopilot-pause": "PAUSE",
                        "autopilot-resume": "RESUME",
                        "autopilot-kill": "KILL",
                    }[arguments.command],
                    reason=arguments.reason,
                    idempotency_key=arguments.idempotency_key,
                )
            )
            return
        if arguments.command == "autopilot-core-demo":
            _print_autopilot(autopilot_core_demo())
            return
        if arguments.command == "paper-portfolio-demo":
            _print_autopilot(paper_portfolio_demo())
            return
        if arguments.command == "strategy-shadow-demo":
            _print_autopilot(strategy_shadow_demo())
            return
        if arguments.command == "orderbook-microstructure-shadow-demo":
            _print_autopilot(orderbook_microstructure_shadow_demo())
            return
        if arguments.command == "market-dynamics-shadow-demo":
            _print_autopilot(market_dynamics_shadow_demo())
            return
        if arguments.command == "multi-strategy-shadow-cycle":
            _print_autopilot(
                multi_strategy_shadow_cycle(
                    campaign_id=arguments.campaign_id,
                    idempotency_key=arguments.idempotency_key,
                    market_limit=arguments.market_limit,
                    maximum_predictions=arguments.maximum_predictions,
                    maximum_opportunities=arguments.maximum_opportunities,
                )
            )
            return
        if arguments.command == "multi-strategy-shadow-session":
            _print_autopilot(
                multi_strategy_shadow_session(
                    campaign_id=arguments.campaign_id,
                    iterations=arguments.iterations,
                    interval_seconds=arguments.interval_seconds,
                    market_limit=arguments.market_limit,
                    maximum_pages=arguments.max_pages,
                    maximum_enrichments=arguments.max_enrichments,
                    orderbook_depth=arguments.orderbook_depth,
                )
            )
            return
        if arguments.command == "reconcile-multi-strategy-shadow-session":
            _print_autopilot(
                reconcile_multi_strategy_shadow_session(session_id=arguments.session_id)
            )
            return
        if arguments.command == "multi-strategy-shadow-demo":
            _print_autopilot(multi_strategy_shadow_demo())
            return
        if arguments.command == "create-shadow-plan":
            _print_autopilot(
                create_shadow_plan(
                    campaign_id=arguments.campaign_id,
                    name=arguments.name,
                    iterations=arguments.iterations,
                    interval_seconds=arguments.interval_seconds,
                    market_count=arguments.market_count,
                    maximum_pages=arguments.max_pages,
                    maximum_enrichments=arguments.max_enrichments,
                    maximum_markets_per_event=arguments.max_markets_per_event,
                    maximum_markets_per_series=arguments.max_markets_per_series,
                    orderbook_depth=arguments.orderbook_depth,
                    idempotency_key=arguments.idempotency_key,
                    market_tickers=_parse_market_tickers(arguments.market_tickers),
                )
            )
            return
        if arguments.command == "discover-create-shadow-plan":
            _print_autopilot(
                discover_create_shadow_plan(
                    campaign_id=arguments.campaign_id,
                    name=arguments.name,
                    window_start=arguments.window_start,
                    window_end=arguments.window_end,
                    iterations=arguments.iterations,
                    interval_seconds=arguments.interval_seconds,
                    target_markets=arguments.target_markets,
                    minimum_markets=arguments.minimum_markets,
                    minimum_events=arguments.minimum_events,
                    maximum_pages=arguments.max_pages,
                    maximum_inspected_markets=arguments.max_inspected_markets,
                    maximum_enrichments=arguments.max_enrichments,
                    maximum_markets_per_event=arguments.max_markets_per_event,
                    maximum_markets_per_series=arguments.max_markets_per_series,
                    orderbook_depth=arguments.orderbook_depth,
                    idempotency_key=arguments.idempotency_key,
                    maximum_orderbook_preflights=arguments.max_orderbook_preflights,
                    microstructure_configuration_version=(
                        arguments.microstructure_configuration_version
                    ),
                    prior_plan_ids=tuple(arguments.exclude_plan_id),
                    exclude_prior_plan_markets=arguments.exclude_prior_plan_markets,
                    exclude_prior_plan_events=arguments.exclude_prior_plan_events,
                    exclude_active_plan_markets=arguments.exclude_active_plan_markets,
                )
            )
            return
        if arguments.command == "run-shadow-plan":
            _print_autopilot(
                shadow_worker(
                    plan_id=arguments.plan_id,
                    once=True,
                    poll_seconds=5,
                    maximum_plans=1,
                )
            )
            return
        if arguments.command == "shadow-worker":
            _print_autopilot(
                shadow_worker(
                    plan_id=arguments.plan_id,
                    once=arguments.once,
                    poll_seconds=arguments.poll_seconds,
                    maximum_plans=arguments.maximum_plans,
                )
            )
            return
        if arguments.command == "collect-shadow-outcomes":
            _print_autopilot(
                collect_shadow_outcomes(
                    campaign_id=arguments.campaign_id,
                    maximum_markets=arguments.market_limit,
                )
            )
            return
        if arguments.command == "strategy-performance":
            _print_autopilot(
                strategy_performance(
                    campaign_id=arguments.campaign_id,
                    strategy_id=arguments.strategy_id,
                    plan_id=arguments.plan_id,
                )
            )
            return
        if arguments.command == "strategy-evidence-snapshot":
            _print_autopilot(
                strategy_evidence_snapshot(
                    strategy_id=arguments.strategy_id,
                    configuration_hash=arguments.configuration_hash,
                    policy_id=arguments.policy_id,
                    cutoff=arguments.cutoff,
                )
            )
            return
        if arguments.command == "request-strategy-review":
            _print_autopilot(
                request_strategy_review(
                    strategy_id=arguments.strategy_id,
                    campaign_id=arguments.campaign_id,
                    snapshot_id=arguments.snapshot_id,
                    reason=arguments.reason,
                    idempotency_key=arguments.idempotency_key,
                )
            )
            return
        if arguments.command == "strategy-review-status":
            _print_autopilot(strategy_review_status(review_id=arguments.review_id))
            return
        if arguments.command == "approve-paper-trial":
            _print_autopilot(
                approve_paper_trial(
                    review_id=arguments.review_id,
                    campaign_id=arguments.campaign_id,
                    starts_at=arguments.starts_at,
                    expires_at=arguments.expires_at,
                    maximum_cycles=arguments.maximum_cycles,
                    maximum_orders=arguments.maximum_orders,
                    maximum_positions=arguments.maximum_positions,
                    maximum_loss=arguments.maximum_loss,
                    confirmation_strategy=arguments.confirmation_strategy,
                    confirmation_campaign=arguments.confirmation_campaign,
                    confirmation_snapshot=arguments.confirmation_snapshot,
                    idempotency_key=arguments.idempotency_key,
                )
            )
            return
        if arguments.command == "evidence-review-paper-trial-demo":
            _print_autopilot(evidence_review_paper_trial_demo())
            return
        if arguments.command == "shadow-operations-demo":
            _print_autopilot(shadow_operations_demo())
            return
        if arguments.command == "create-shadow-campaign":
            _print_autopilot(
                create_shadow_campaign(
                    name=arguments.name,
                    capital=arguments.capital,
                    maximum_loss=arguments.maximum_loss,
                    expires_in_days=arguments.expires_in_days,
                    idempotency_key=arguments.idempotency_key,
                )
            )
            return
        if arguments.command == "paper-portfolio-status":
            _print_autopilot(paper_portfolio_status(arguments.campaign_id))
            return
        if arguments.command == "paper-mark-positions":
            _print_autopilot(
                paper_mark_positions(arguments.campaign_id, arguments.orderbook_run_id)
            )
            return
        if arguments.command == "paper-settle-position":
            _print_autopilot(
                {
                    "position_id": arguments.position_id,
                    "settlement_id": paper_settle_position(
                        arguments.position_id, arguments.settlement_fact_id
                    ),
                }
            )
            return
        if arguments.command == "strategy-authority":
            _print_autopilot(
                change_strategy_authority(
                    strategy_id=arguments.strategy_id,
                    requested_authority=arguments.requested_authority,
                    operator_reason=arguments.operator_reason,
                    evidence_review_id=arguments.evidence_review_id,
                    idempotency_key=arguments.idempotency_key,
                    confirmation_text=arguments.confirmation_text,
                )
            )
            return
        if arguments.command == "live-shadow-session":
            _print_autopilot(
                live_shadow_session(
                    campaign_id=arguments.campaign_id,
                    strategy_id=arguments.strategy_id,
                    iterations=arguments.iterations,
                    interval_seconds=arguments.interval_seconds,
                    market_limit=arguments.market_limit,
                    maximum_pages=arguments.maximum_pages,
                    maximum_enrichments=arguments.maximum_enrichments,
                    observations_per_market=arguments.observations_per_market,
                    orderbook_depth=arguments.orderbook_depth,
                )
            )
            return
        if arguments.command == "scan-kalshi-fixture":
            _print_public_market_summary(scan_kalshi_fixture(persist=arguments.persist))
            return
        if arguments.command == "smoke-kalshi-public":
            _print_public_market_summary(
                smoke_kalshi_public(
                    limit=arguments.limit,
                    maximum_pages=arguments.max_pages,
                    timeout_seconds=arguments.timeout_seconds,
                    persist=arguments.persist,
                )
            )
            return
        if arguments.command == "scan-kalshi-consistency-fixture":
            _print_public_event_consistency_summary(
                scan_kalshi_consistency_fixture(persist=arguments.persist)
            )
            return
        if arguments.command == "smoke-kalshi-consistency":
            _print_public_event_consistency_summary(
                smoke_kalshi_consistency(
                    event_limit=arguments.event_limit,
                    market_limit=arguments.market_limit,
                    maximum_pages=arguments.max_pages,
                    timeout_seconds=arguments.timeout_seconds,
                    persist=arguments.persist,
                )
            )
            return
        if arguments.command == "scan-kalshi-binary-complements-fixture":
            _print_binary_complement_report(scan_kalshi_binary_complements_fixture())
            return
        if arguments.command == "capture-kalshi-orderbook-series-fixture":
            _print_capture_report(capture_kalshi_orderbook_series_fixture())
            return
        if arguments.command == "capture-kalshi-orderbook-series":
            _print_capture_report(
                capture_kalshi_orderbook_series(
                    market_ticker=arguments.market_ticker,
                    latest_open_market=arguments.latest_open_market,
                    discover_first=arguments.discover_first,
                    observations=arguments.observations,
                    interval_seconds=arguments.interval_seconds,
                    maximum_depth=arguments.depth,
                    maximum_bundle_quantity=arguments.max_bundle_contracts,
                    timeout_seconds=arguments.timeout_seconds,
                )
            )
            return
        if arguments.command == "sweep-kalshi-orderbooks-fixture":
            _print_sweep_report(sweep_kalshi_orderbooks_fixture())
            return
        if arguments.command == "sweep-kalshi-orderbooks":
            _print_sweep_report(
                sweep_kalshi_orderbooks(
                    market_count=arguments.market_count,
                    maximum_pages=arguments.max_pages,
                    market_limit=arguments.market_limit,
                    maximum_enrichments=arguments.max_enrichments,
                    observations=arguments.observations,
                    interval_seconds=arguments.interval_seconds,
                    maximum_depth=arguments.depth,
                    maximum_bundle_quantity=arguments.max_bundle_contracts,
                    timeout_seconds=arguments.timeout_seconds,
                )
            )
            return
        if arguments.command == "scan-kalshi-threshold-ladders-fixture":
            _print_threshold_ladder_report(scan_kalshi_threshold_ladders_fixture())
            return
        if arguments.command == "scan-kalshi-threshold-ladders":
            _print_threshold_ladder_report(
                scan_kalshi_threshold_ladders(
                    scan_id=arguments.scan_id,
                    event_count=arguments.event_count,
                    maximum_pages=arguments.max_pages,
                    market_limit=arguments.market_limit,
                    maximum_enrichments=arguments.max_enrichments,
                    maximum_markets_per_event=arguments.max_markets_per_event,
                    maximum_relationships=arguments.max_relationships,
                    rounds=arguments.rounds,
                    interval_seconds=arguments.interval_seconds,
                    maximum_depth=arguments.depth,
                    maximum_bundle_quantity=arguments.max_bundle_contracts,
                    maximum_observation_skew_seconds=arguments.max_observation_skew_seconds,
                    timeout_seconds=arguments.timeout_seconds,
                )
            )
            return
        if arguments.command == "diagnose-kalshi-capture-selection":
            diagnose_kalshi_capture_selection(
                max_pages=arguments.max_pages,
                market_limit=arguments.market_limit,
                timeout_seconds=arguments.timeout_seconds,
            )
            return
        if arguments.command == "reconcile-kalshi-capture-sessions":
            sessions = reconcile_kalshi_capture_sessions()
            print("reconciled_sessions=" + (",".join(sessions) or "none"))
            return
        if arguments.command == "smoke-kalshi-binary-complements":
            _print_binary_complement_report(
                smoke_kalshi_binary_complements(
                    market_run_id=arguments.market_run_id,
                    market_limit=arguments.market_limit,
                    maximum_depth=arguments.depth,
                    maximum_bundle_quantity=arguments.max_bundle_contracts,
                    timeout_seconds=arguments.timeout_seconds,
                    discover_first=arguments.discover_first,
                )
            )
            return
        if arguments.command == "compare-kalshi-scans-fixture":
            _print_scan_comparison_report(compare_kalshi_scans_fixture())
            return
        if arguments.command == "compare-kalshi-scans":
            _print_scan_comparison_report(
                compare_kalshi_scans(
                    baseline_run_id=arguments.baseline_run_id,
                    current_run_id=arguments.current_run_id,
                )
            )
            return
        if arguments.command == "compare-kalshi-latest-scans":
            _print_scan_comparison_report(compare_kalshi_latest_scans())
            return
        if arguments.command == "summarize-kalshi-event-history-fixture":
            _print_event_history_report(summarize_kalshi_event_history_fixture())
            return
        if arguments.command == "summarize-kalshi-event-history":
            _print_event_history_report(
                summarize_kalshi_event_history(
                    event_ticker=arguments.event_ticker,
                    maximum_completed_runs=arguments.max_runs,
                    first_ingestion_run_id=arguments.first_run_id,
                    last_ingestion_run_id=arguments.last_run_id,
                    continuity_gap_seconds=arguments.continuity_gap_seconds,
                )
            )
            return
        if arguments.command == "summarize-kalshi-orderbook-history-fixture":
            _print_orderbook_history_report(summarize_kalshi_orderbook_history_fixture())
            return
        if arguments.command == "summarize-kalshi-orderbook-history":
            _print_orderbook_history_report(
                summarize_kalshi_orderbook_history(
                    market_ticker=arguments.market_ticker,
                    maximum_observations=arguments.max_observations,
                    first_orderbook_snapshot_id=arguments.first_snapshot_id,
                    last_orderbook_snapshot_id=arguments.last_snapshot_id,
                    continuity_gap_seconds=arguments.continuity_gap_seconds,
                )
            )
            return
    except (
        PublicMarketDiscoveryError,
        PublicEventDiscoveryError,
        PublicScanComparisonError,
        EventObservationHistoryError,
        OrderbookObservationHistoryError,
        PublicBinaryComplementError,
        PublicOrderbookCaptureError,
        AutopilotError,
    ) as error:
        raise SystemExit(str(error)) from error
    raise SystemExit("unsupported command")


if __name__ == "__main__":
    main()
