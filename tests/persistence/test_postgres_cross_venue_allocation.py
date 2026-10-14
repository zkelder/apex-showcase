from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
    PostgresCrossVenueAllocationSource,
)
from apex.adapters.crypto_paper_postgres import (
    account_table as crypto_account_table,
)
from apex.adapters.crypto_paper_postgres import (
    capability_table as crypto_capability_table,
)
from apex.adapters.kalshi_operating_postgres import (
    account_table as kalshi_account_table,
)
from apex.adapters.kalshi_operating_postgres import (
    capability_table as kalshi_capability_table,
)
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.application.cross_venue_allocation import MaterializeCrossVenueAllocation
from apex.application.shared_opportunity import MaterializeSharedOpportunitySet
from apex.application.total_market_context import (
    AttachTotalMarketContext,
    MaterializeTotalMarketContextSnapshot,
)
from apex.domain.cross_venue_allocation import CrossVenueAllocationError
from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperCapability,
    ProviderCapabilityState,
)
from apex.domain.kalshi_operating import KalshiCapability, KalshiPaperAccount
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskStatus,
    OperatingTaskType,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.total_market_context import (
    ContextComponent,
    ContextComponentType,
    ContextSourceBundle,
    UnavailableContextComponent,
)
from apex.domain.values import canonical_json_bytes, content_digest

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _projection(identity: str, venue: SharedVenue) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"cross-postgres-{identity}",
        venue=venue,
        source_record_type="CrossVenueFixtureOpportunity",
        source_record_id=f"cross-source-{identity}",
        source_canonical_hash=content_digest((identity, "cross-source")),
        strategy_or_method_id=f"cross-method-{identity}",
        instrument_id=f"cross-instrument-{identity}",
        product_or_market_id=f"cross-market-{identity}",
        direction="BUY_YES" if venue is SharedVenue.KALSHI else "BUY",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=2),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=30,
        freshness_limit_seconds=3600,
        comparable_value="0.08" if venue is SharedVenue.CRYPTO else "0.06",
        comparable_value_identity="FIXTURE_COMPARABLE_VALUE",
        uncertainty="0.1",
        maximum_loss="5",
        modeled_fees="0.25",
        modeled_slippage="0.25",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="25",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.20",
        expected_holding_days=5,
        estimated_lockup_days=5,
        supporting_evidence_ids=(f"cross-evidence-{identity}",),
        source_warnings=("FIXTURE_ONLY",),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("FIXTURE_ONLY",),
        source_details=(("fixture", identity),),
    )


class _SharedSource:
    def crypto_projections(self, *, pulse_id: str, as_of: datetime):
        return (_projection("crypto", SharedVenue.CRYPTO),)

    def kalshi_projections(self, *, evaluation_ids: tuple[str, ...], as_of: datetime):
        return (_projection("kalshi", SharedVenue.KALSHI),)

    def securities_projections(self, *, as_of: datetime):
        return ()


class _ContextSource:
    def load(self, *, source_batch_id: str, as_of: datetime) -> ContextSourceBundle:
        equity = ContextComponent.create(
            component_id="cross-postgres-equity-context",
            component_type=ContextComponentType.US_EQUITY_MARKET,
            source_observation_id="cross-postgres-spy-bar",
            provider="FIXTURE_RETAINED",
            subject="SPY",
            observation_at=NOW - timedelta(minutes=2),
            available_at=NOW - timedelta(minutes=1),
            ingested_at=NOW - timedelta(seconds=30),
            current_value="101",
            prior_comparable_value="100",
            freshness_seconds=60,
            freshness_limit_seconds=3600,
            quality_status="VALID",
            evidence_class="FIXTURE",
            limitations=("FIXTURE_ONLY",),
            interpretation_category="SPY_FIXTURE_CONTEXT",
            supporting_evidence_ids=("cross-postgres-context-evidence",),
        )
        volatility = ContextComponent.create(
            component_id="cross-postgres-volatility-context",
            component_type=ContextComponentType.MARKET_VOLATILITY,
            source_observation_id="cross-postgres-volatility-bar",
            provider="FIXTURE_RETAINED",
            subject="SPY",
            observation_at=NOW - timedelta(minutes=2),
            available_at=NOW - timedelta(minutes=1),
            ingested_at=NOW - timedelta(seconds=30),
            current_value="0.10",
            prior_comparable_value="0.12",
            freshness_seconds=60,
            freshness_limit_seconds=3600,
            quality_status="VALID",
            evidence_class="FIXTURE",
            limitations=("FIXTURE_ONLY",),
            interpretation_category="SPY_VOLATILITY_FIXTURE_CONTEXT",
            supporting_evidence_ids=("cross-postgres-volatility-evidence",),
        )
        components = (equity, volatility)
        return ContextSourceBundle(
            components,
            tuple(
                UnavailableContextComponent(value, "FIXTURE_UNAVAILABLE")
                for value in ContextComponentType
                if value not in {item.component_type for item in components}
            ),
        )


def _retain_paper_readiness(engine: sa.Engine) -> None:
    crypto_capability = CryptoPaperCapability(
        capability_id="cross-postgres-crypto-capability",
        contract_id=None,
        primary_state=ProviderCapabilityState.PAPER_ONLY,
        states=(ProviderCapabilityState.PAPER_ONLY,),
        credentials_requested=False,
        provider_requests=0,
        inspected_at=NOW,
        limitations=("FIXTURE_ONLY",),
        canonical_hash="a" * 64,
    )
    crypto_account = CryptoPaperAccount.create(
        account_id="cross-postgres-crypto-account", opening_cash="1000", created_at=NOW
    )
    kalshi_capability = KalshiCapability.inspect(
        capability_id="cross-postgres-kalshi-capability",
        inspected_at=NOW,
        public_discovery=True,
        public_orderbook=True,
        public_rules=True,
    )
    kalshi_account = KalshiPaperAccount.create(
        account_id="cross-postgres-kalshi-account", opening_cash="1000", created_at=NOW
    )
    with engine.begin() as connection:
        connection.execute(
            crypto_capability_table.insert().values(
                capability_id=crypto_capability.capability_id,
                contract_id=None,
                idempotency_key="cross-postgres-crypto-capability-key",
                inspected_at=NOW,
                payload=canonical_json_bytes(crypto_capability).decode("utf-8"),
                canonical_hash=crypto_capability.canonical_hash,
                provider_requests=0,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )
        connection.execute(
            crypto_account_table.insert().values(
                account_id=crypto_account.account_id,
                created_at=NOW,
                idempotency_key="cross-postgres-crypto-account-key",
                payload=canonical_json_bytes(crypto_account).decode("utf-8"),
                canonical_hash=crypto_account.canonical_hash,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )
        connection.execute(
            kalshi_capability_table.insert().values(
                capability_id=kalshi_capability.capability_id,
                idempotency_key="cross-postgres-kalshi-capability-key",
                inspected_at=NOW,
                payload=canonical_json_bytes(kalshi_capability).decode("utf-8"),
                canonical_hash=kalshi_capability.canonical_hash,
                provider_requests=0,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )
        connection.execute(
            kalshi_account_table.insert().values(
                account_id=kalshi_account.account_id,
                created_at=NOW,
                opening_cash=kalshi_account.opening_cash,
                idempotency_key="cross-postgres-kalshi-account-key",
                payload=canonical_json_bytes(kalshi_account).decode("utf-8"),
                canonical_hash=kalshi_account.canonical_hash,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )


def _source_evidence(engine: sa.Engine) -> str:
    shared = PostgresSharedOpportunityRepository(engine)
    shared_result = MaterializeSharedOpportunitySet(shared, _SharedSource()).execute(
        opportunity_set_id="cross-postgres-shared-set",
        crypto_pulse_id="cross-postgres-pulse",
        kalshi_evaluation_ids=("cross-postgres-evaluation",),
        as_of=NOW,
        idempotency_key="cross-postgres-shared-key",
    )
    context = PostgresTotalMarketContextRepository(engine)
    context_result = MaterializeTotalMarketContextSnapshot(context, _ContextSource()).execute(
        snapshot_id="cross-postgres-context",
        source_batch_id="cross-postgres-context-batch",
        as_of=NOW,
        idempotency_key="cross-postgres-context-key",
    )
    enriched = AttachTotalMarketContext(context).execute(
        enriched_set_id="cross-postgres-enriched",
        opportunity_set_id=shared_result.opportunity_set.opportunity_set_id,
        context_snapshot_id=context_result.snapshot.snapshot_id,
        attached_at=NOW,
        idempotency_key="cross-postgres-enriched-key",
    )
    return enriched.enriched_set.enriched_set_id


def test_cross_venue_bundle_is_atomic_immutable_idempotent_and_nonfinancial(
    postgres_engine: sa.Engine,
) -> None:
    before_tables = (
        '"order"',
        "position",
        "risk_reservation",
        "execution_event",
        "ledger_journal",
        "ledger_posting",
    )
    with postgres_engine.connect() as connection:
        before = {
            table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
            for table in before_tables
        }
    empty_workspace = PostgresCrossVenueAllocationRepository(postgres_engine).workspace()
    assert empty_workspace["decision"] is None
    assert empty_workspace["paper_trial_proposals"] == ()
    enriched_set_id = _source_evidence(postgres_engine)
    _retain_paper_readiness(postgres_engine)
    repository = PostgresCrossVenueAllocationRepository(postgres_engine)
    service = MaterializeCrossVenueAllocation(
        repository, PostgresCrossVenueAllocationSource(postgres_engine)
    )
    first = service.execute(
        decision_id="cross-postgres-decision",
        enriched_set_id=enriched_set_id,
        decided_at=NOW,
        idempotency_key="cross-postgres-decision-key",
    )
    replay = service.execute(
        decision_id="cross-postgres-decision",
        enriched_set_id=enriched_set_id,
        decided_at=NOW,
        idempotency_key="cross-postgres-decision-key",
    )
    assert not first.idempotent_replay and replay.idempotent_replay
    assert len(first.lines) == len(first.proposals) == 2
    assert all(not item.authorization and not item.order for item in first.proposals)
    assert repository.decision(first.decision.decision_id) == first.decision
    populated_workspace = repository.workspace()
    assert populated_workspace["decision"] == first.decision
    assert len(populated_workspace["paper_trial_proposals"]) == 2
    with pytest.raises(CrossVenueAllocationError, match="IDENTITY_CONFLICT"):
        service.execute(
            decision_id="cross-postgres-decision",
            enriched_set_id=enriched_set_id,
            decided_at=NOW + timedelta(seconds=1),
            idempotency_key="cross-postgres-decision-key",
        )
    with pytest.raises(sa.exc.DBAPIError), postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE cross_venue_allocation_decision "
                "SET authority = 'PAPER_ONLY' WHERE decision_id = :decision_id"
            ),
            {"decision_id": first.decision.decision_id},
        )
    with postgres_engine.connect() as connection:
        after = {
            table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
            for table in before_tables
        }
    assert before == after
    with postgres_engine.connect() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        # Downgrading now hits migration 0093's own retained-evidence guard
        # first (it is newer than 0063 and this test's own _source_evidence
        # genuinely retains a total_market_context_component row via
        # MaterializeTotalMarketContextSnapshot) -- 0063's own guard,
        # further down the chain, is never reached from head. See
        # test_postgres_total_market_context.py::
        # test_downgrade_0093_blocks_with_retained_component_evidence for a
        # direct, single-step proof of 0093's own guard.
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0093"):
            command.downgrade(config, "0062_kalshi_operating_paper")
        connection.rollback()


def test_operating_cycle_allocation_task_delegates_to_cross_venue_seam(
    postgres_engine: sa.Engine,
) -> None:
    enriched_set_id = _source_evidence(postgres_engine)
    _retain_paper_readiness(postgres_engine)
    operating = PostgresOperatingCycleRepository(postgres_engine)
    state_source = PostgresOperatingStateSource(postgres_engine, operating)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    plan = OperatingCyclePlan.create(
        plan_id="cross-postgres-operating-plan",
        crypto_pulse_plan_id="cross-postgres-pulse-plan",
        actor="fixture-operator",
        created_at=NOW,
    )
    task = DueTask.create(
        task_type=OperatingTaskType.RESEARCH_ALLOCATION_REBUILD,
        disposition=DueDisposition.DUE,
        reasons=("CONTEXT_ENRICHED_SET_CHANGED_OR_ALLOCATION_MISSING",),
        dependency_types=(OperatingTaskType.CONTEXT_ENRICHMENT_REBUILD,),
        source_evidence_ids=(enriched_set_id,),
        cycle_id="cross-postgres-cycle",
        network_behavior="RETAINED_EVIDENCE_ONLY",
    )
    result = executor.execute(
        task=task,
        cycle_id="cross-postgres-cycle",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    repository = PostgresCrossVenueAllocationRepository(postgres_engine)
    decision = repository.decision("cross-postgres-cycle-cross-venue-allocation")
    assert result.status is OperatingTaskStatus.COMPLETED
    assert result.network_requests == 0
    assert decision is not None
    assert len(decision.paper_trial_proposal_ids) == 2
    assert "NO_PAPER_AUTHORIZATION_OR_ORDER_CREATED" in result.warnings
