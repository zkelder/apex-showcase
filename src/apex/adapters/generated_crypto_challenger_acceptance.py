"""Genuine retained-state acceptance for the generated Crypto challenger."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.engine import Engine

from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
    PostgresCrossVenueAllocationSource,
)
from apex.adapters.crypto_market_pulse_postgres import PostgresCryptoMarketPulseRepository
from apex.adapters.crypto_stateful_research_postgres import (
    PostgresCryptoStatefulResearchRepository,
)
from apex.adapters.crypto_stateful_research_sources import (
    CoinbasePublicExecutionHistorySource,
)
from apex.adapters.first_generated_research_method_sources import (
    PostgresCurrentOpportunityOperationalEvidenceSource,
)
from apex.adapters.research_integrity_postgres import PostgresResearchIntegrityRepository
from apex.adapters.research_operations_executor import (
    ExistingResearchLedgerExecutor,
    FirstGeneratedOperationalMethodExecutor,
    GovernedResearchExecutor,
)
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
    PostgresRetainedResearchKnowledgeSource,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.application.cross_venue_allocation import MaterializeCrossVenueAllocation
from apex.application.crypto_market_pulse import current_crypto_method_inputs
from apex.application.crypto_stateful_research import (
    CaptureCryptoExecutionHistories,
    CryptoStatefulExperimentExecutor,
    ImplementCryptoStatefulMethod,
    RefineAndApproveGeneratedCryptoProposal,
    RunCurrentCryptoStatefulMethod,
)
from apex.application.research_operations import (
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
)
from apex.application.shared_opportunity import MaterializeSharedOpportunitySet
from apex.application.total_market_context import AttachTotalMarketContext
from apex.domain.crypto_universe import (
    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
    CryptoDiscoveryRun,
)
from apex.domain.shared_opportunity import SharedVenue

ORIGINAL_PROPOSAL_ID = (
    "generated-genuine-research-proposal-generation-2026-08-02-v1-"
    "agenda-negative_expected_economics-current-opportunity-method-pack-v2-crypto-2026-08-02-v1"
)
REFINED_PROPOSAL_ID = f"{ORIGINAL_PROPOSAL_ID}-refinement-v2"
REFINEMENT_ID = "generated-crypto-challenger-refinement-2026-08-03-v1"
SPECIFICATION_ID = "crypto-multi-asset-stateful-pullback-rotation-specification-v1"
IMPLEMENTATION_ID = "crypto-stateful-one-position-research-primitive-v1"
DATASET_ID = "generated-crypto-challenger-open-close-history-2026-08-03-v1"
QUEUE_ID = "generated-crypto-challenger-queue-2026-08-03-v1"
RUN_ID = "generated-crypto-challenger-experiment-2026-08-03-v1"
SIGNAL_ID = "generated-crypto-challenger-current-signal-2026-08-03-v1"
SHARED_SET_ID = "generated-crypto-challenger-shared-2026-08-03-v1"
ENRICHED_SET_ID = "generated-crypto-challenger-enriched-2026-08-03-v1"
ALLOCATION_ID = "generated-crypto-challenger-allocation-2026-08-03-v1"
PHASE_AT = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def _comparison(run: CryptoDiscoveryRun, horizon_index: int = 0) -> Decimal | None:
    calibration = run.calibration
    if calibration is None:
        return None
    top = next((value for value in calibration.buckets if value.bucket == "TOP_THIRD"), None)
    if top is None:
        return None
    if top.horizon_results:
        return top.horizon_results[horizon_index].expected_net_return
    return top.expected_net_return


def _has_complete_730_day_coverage(run: CryptoDiscoveryRun) -> bool:
    calibration = run.calibration
    return (
        calibration is not None
        and len(calibration.asset_coverage) == 10
        and all(item.accepted_buckets == 730 for item in calibration.asset_coverage)
    )


def run_generated_crypto_challenger_acceptance(engine: Engine) -> dict[str, object]:
    operations = PostgresResearchOperationsRepository(engine)
    knowledge = PostgresRetainedResearchKnowledgeSource(engine)
    stateful = PostgresCryptoStatefulResearchRepository(engine)
    crypto = PostgresCryptoMarketPulseRepository(engine)
    original = operations.proposal(ORIGINAL_PROPOSAL_ID)
    if original is None:
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_ORIGINAL_PROPOSAL_MISSING")
    original_feasibilities = tuple(
        value
        for value in operations.proposal_feasibilities()
        if value.proposal_id == ORIGINAL_PROPOSAL_ID
    )
    reversal_run = next(
        value
        for value in reversed(crypto.discovery_runs())
        if value.method_id == CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1
        and _has_complete_730_day_coverage(value)
    )
    relative_run = next(
        value
        for value in reversed(crypto.discovery_runs())
        if value.method_id == CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1
        and _has_complete_730_day_coverage(value)
    )
    if reversal_run.source_pulse_id is None:
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_PULSE_LINEAGE_MISSING")
    pulse = crypto.pulse_snapshot(reversal_run.source_pulse_id)
    if pulse is None:
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_PULSE_MISSING")
    inputs = current_crypto_method_inputs(crypto, pulse=pulse, generated_at=PHASE_AT)
    reversal_calibration = reversal_run.calibration
    if reversal_calibration is None:
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_CALIBRATION_MISSING")
    observations = tuple(
        crypto.observation(value.observation_id) for value in reversal_calibration.asset_coverage
    )
    if any(value is None for value in observations):
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_HISTORY_MISSING")
    retained_observations = tuple(value for value in observations if value is not None)
    refinement_result = RefineAndApproveGeneratedCryptoProposal(
        operations, stateful, knowledge
    ).execute(
        original_proposal_id=ORIGINAL_PROPOSAL_ID,
        refined_proposal_id=REFINED_PROPOSAL_ID,
        refinement_id=REFINEMENT_ID,
        dataset_id=DATASET_ID,
        retained_observation_ids=tuple(value.observation_id for value in retained_observations),
        current_evidence_ids=(
            pulse.pulse_id,
            reversal_run.run_id,
            relative_run.run_id,
        ),
        actor="operator:genuine-acceptance",
        refined_at=PHASE_AT,
        provider_request_limit=30,
    )
    dataset = CaptureCryptoExecutionHistories(
        operations,
        stateful,
        CoinbasePublicExecutionHistorySource(),
    ).execute(
        proposal_id=REFINED_PROPOSAL_ID,
        dataset_id=DATASET_ID,
        observations=retained_observations,
        requested_at=PHASE_AT,
    )
    specification, feasibility, _ = ImplementCryptoStatefulMethod(
        operations, stateful, knowledge
    ).execute(
        proposal_id=REFINED_PROPOSAL_ID,
        specification_id=SPECIFICATION_ID,
        implementation_id=IMPLEMENTATION_ID,
        implemented_at=PHASE_AT.replace(minute=1),
        histories=dataset.histories,
    )
    executor = GovernedResearchExecutor(
        ledger=ExistingResearchLedgerExecutor(PostgresResearchIntegrityRepository(engine)),
        operational=FirstGeneratedOperationalMethodExecutor(
            operations, PostgresCurrentOpportunityOperationalEvidenceSource(engine)
        ),
        stateful_crypto=CryptoStatefulExperimentExecutor(
            stateful,
            specification,
            dataset.histories,
            DATASET_ID,
            _comparison(relative_run),
            _comparison(reversal_run),
        ),
    )
    enqueued = EnqueueResearchExperiment(operations).execute(
        queue_id=QUEUE_ID,
        proposal_id=REFINED_PROPOSAL_ID,
        run_id=RUN_ID,
        actor="operator:genuine-acceptance",
        idempotency_key="generated-crypto-challenger-enqueue-v1",
        enqueued_at=PHASE_AT.replace(minute=2),
    )
    executed = ExecuteApprovedResearchQueueItem(operations, executor, knowledge).execute(
        queue_id=QUEUE_ID,
        actor="operator:genuine-acceptance",
        idempotency_key="generated-crypto-challenger-execute-v1",
        executed_at=PHASE_AT.replace(minute=3),
    )
    experiment = stateful.experiment_run(RUN_ID)
    if experiment is None:
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_EXPERIMENT_MISSING")
    signal, source_opportunities, signal_replay = RunCurrentCryptoStatefulMethod(
        stateful, crypto
    ).execute(
        signal_id=SIGNAL_ID,
        specification=specification,
        experiment=experiment,
        recommendation_id=executed.governed_result.recommendation.recommendation_id,
        source_pulse_id=pulse.pulse_id,
        universe_snapshot_id=inputs.snapshot.snapshot_id,
        observations=inputs.observations,
        current_market=inputs.market,
        generated_at=PHASE_AT.replace(minute=4),
    )
    shared_repository = PostgresSharedOpportunityRepository(engine)
    shared_source = RetainedSharedOpportunitySource(engine)
    existing_sets = shared_repository.opportunity_sets()
    kalshi_ids: tuple[str, ...] = ()
    if existing_sets:
        latest = existing_sets[-1]
        kalshi_ids = tuple(
            projection.source_record_id
            for projection_id in latest.projection_ids
            if (projection := shared_repository.projection(projection_id)) is not None
            and projection.venue is SharedVenue.KALSHI
            and projection.source_record_type == "BinaryComplementEvaluation"
        )
    shared = MaterializeSharedOpportunitySet(shared_repository, shared_source).execute(
        opportunity_set_id=SHARED_SET_ID,
        crypto_pulse_id=pulse.pulse_id,
        kalshi_evaluation_ids=kalshi_ids,
        as_of=PHASE_AT.replace(minute=5),
        idempotency_key="generated-crypto-challenger-shared-v1",
    )
    context_repository = PostgresTotalMarketContextRepository(engine)
    context_snapshots = context_repository.context_snapshots()
    if not context_snapshots:
        raise ValueError("GENERATED_CRYPTO_CHALLENGER_CONTEXT_MISSING")
    enriched = AttachTotalMarketContext(context_repository).execute(
        enriched_set_id=ENRICHED_SET_ID,
        opportunity_set_id=SHARED_SET_ID,
        context_snapshot_id=context_snapshots[-1].snapshot_id,
        attached_at=PHASE_AT.replace(minute=6),
        idempotency_key="generated-crypto-challenger-context-v1",
    )
    allocation = MaterializeCrossVenueAllocation(
        PostgresCrossVenueAllocationRepository(engine),
        PostgresCrossVenueAllocationSource(engine),
    ).execute(
        decision_id=ALLOCATION_ID,
        enriched_set_id=enriched.enriched_set.enriched_set_id,
        decided_at=PHASE_AT.replace(minute=7),
        idempotency_key="generated-crypto-challenger-allocation-v1",
    )
    refined_feasibilities = tuple(
        value
        for value in operations.proposal_feasibilities()
        if value.proposal_id == REFINED_PROPOSAL_ID
    )
    shared_eligible = tuple(value for value in shared.decisions if value.state.value == "ELIGIBLE")
    return {
        "original_proposal": original,
        "original_feasibilities": original_feasibilities,
        "refined_proposal": refinement_result.proposal,
        "refinement": refinement_result.refinement,
        "approval": operations.experiment_approval(REFINED_PROPOSAL_ID),
        "refined_feasibilities": refined_feasibilities,
        "specification": specification,
        "dataset_id": DATASET_ID,
        "dataset_hash": dataset.canonical_hash,
        "dataset_request_count": dataset.request_count,
        "dataset_history_ids": tuple(value.history_id for value in dataset.histories),
        "queue_item": enqueued.item,
        "queue_event": executed.event,
        "budget": executed.governed_result.budget,
        "experiment": experiment,
        "recommendation": executed.governed_result.recommendation,
        "current_signal": signal,
        "source_opportunities": source_opportunities,
        "shared_set": shared.opportunity_set,
        "shared_eligible_ids": tuple(value.projection_id for value in shared_eligible),
        "shared_priorities": shared.priorities,
        "enriched_set": enriched.enriched_set,
        "allocation": allocation.decision,
        "allocation_lines": allocation.lines,
        "paper_trial_proposals": allocation.proposals,
        "replay": {
            "refinement": refinement_result.idempotent_replay,
            "dataset": dataset.idempotent_replay,
            "queue": enqueued.idempotent_replay,
            "experiment": executed.idempotent_replay,
            "signal": signal_replay,
        },
        "feasibility_after": feasibility,
        "provider_writes": 0,
        "automatic_paper_actions": 0,
        "financial_mutations": 0,
        "live_authority": "NONE",
    }
