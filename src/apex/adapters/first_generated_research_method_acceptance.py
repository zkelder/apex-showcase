"""Genuine retained-evidence acceptance for the first generated research method."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

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
from apex.application.first_generated_research_method import (
    SelectApproveAndImplementFirstGeneratedMethod,
)
from apex.application.research_operations import (
    EnqueueResearchExperiment,
    ExecuteApprovedResearchQueueItem,
    proposal_state,
)

GENERATOR_RUN_ID = "genuine-research-proposal-generation-2026-08-02-v1"
SELECTION_ID = "first-generated-research-method-selection-2026-08-03-v1"
IMPLEMENTATION_ID = "paper-readiness-existing-method-operationalization-v1"
QUEUE_ID = "first-generated-research-method-queue-v1"
RUN_ID = "first-generated-research-method-run-v1"
ACCEPTED_AT = datetime(2026, 8, 3, 16, 50, tzinfo=UTC)

_FINANCIAL_TABLES = (
    "campaign",
    "opportunity_proposal",
    "allocation_decision",
    "risk_decision",
    "order",
    "risk_reservation",
    "position",
    "execution_event",
    "ledger_journal",
    "ledger_posting",
    "apex_crypto_paper_intent",
    "apex_crypto_paper_order",
    "apex_kalshi_paper_order",
    "external_paper_submission",
)


def _counts(engine: Engine) -> tuple[tuple[str, int], ...]:
    available = set(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        return tuple(
            (table, int(connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) or 0))
            for table in _FINANCIAL_TABLES
            if table in available
        )


def run_genuine_first_generated_method_acceptance(engine: Engine) -> dict[str, object]:
    """Approve one genuine proposal, run it once, and prove exact replay is free."""

    repository = PostgresResearchOperationsRepository(engine)
    knowledge = PostgresRetainedResearchKnowledgeSource(engine)
    before = _counts(engine)
    selection = SelectApproveAndImplementFirstGeneratedMethod(repository, knowledge).execute(
        generator_run_id=GENERATOR_RUN_ID,
        selection_id=SELECTION_ID,
        implementation_id=IMPLEMENTATION_ID,
        actor="operator:genuine-acceptance",
        selected_at=ACCEPTED_AT,
    )
    enqueued = EnqueueResearchExperiment(repository).execute(
        queue_id=QUEUE_ID,
        proposal_id=selection.selection.selected_proposal_id,
        run_id=RUN_ID,
        actor="operator:genuine-acceptance",
        idempotency_key="first-generated-method-enqueue-v1",
        enqueued_at=ACCEPTED_AT,
    )
    evidence_source = PostgresCurrentOpportunityOperationalEvidenceSource(engine)
    executor = GovernedResearchExecutor(
        ledger=ExistingResearchLedgerExecutor(PostgresResearchIntegrityRepository(engine)),
        operational=FirstGeneratedOperationalMethodExecutor(repository, evidence_source),
    )
    runner = ExecuteApprovedResearchQueueItem(repository, executor, knowledge)
    executed = runner.execute(
        queue_id=QUEUE_ID,
        actor="operator:genuine-acceptance",
        idempotency_key="first-generated-method-execute-v1",
        executed_at=ACCEPTED_AT,
    )
    replay = runner.execute(
        queue_id=QUEUE_ID,
        actor="operator:genuine-acceptance",
        idempotency_key="first-generated-method-replay-v1",
        executed_at=ACCEPTED_AT,
    )
    method_run = repository.first_method_runs()[-1]
    after = _counts(engine)
    deferred_states = tuple(
        (item.proposal_id, proposal_state(repository, item.proposal_id))
        for item in selection.selection.deferred
    )
    approved = tuple(
        value.proposal_id
        for value in repository.proposals()
        if repository.experiment_approval(value.proposal_id) is not None
    )
    generator = repository.generator_run(GENERATOR_RUN_ID)
    if generator is None:
        raise RuntimeError("genuine generator run disappeared during acceptance")
    return {
        "selection": selection.selection,
        "implementation": selection.implementation,
        "prior_feasibility_id": selection.feasibility.supersedes_feasibility_id,
        "implemented_feasibility": selection.feasibility,
        "approved_generated_proposal_ids": tuple(
            value for value in approved if value in generator.generated_proposal_ids
        ),
        "deferred_states": deferred_states,
        "queue": enqueued.item,
        "queue_completion": executed.event,
        "method_run": method_run,
        "recommendation": executed.governed_result.recommendation,
        "budget": replay.governed_result.budget,
        "exact_replay": replay.idempotent_replay,
        "financial_counts_before": before,
        "financial_counts_after": after,
        "financial_state_unchanged": before == after,
        "provider_requests": method_run.provider_request_count,
        "paper_authorizations": 0,
        "live_authority": "NONE",
    }
