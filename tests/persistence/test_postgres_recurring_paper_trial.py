"""Persistence acceptance for Recurring Genuine PAPER Evidence and Reconciliation v1.

These are engineering-acceptance fixtures over a real PostgreSQL engine, not
genuine accumulated market evidence: the Shared Opportunity -> Total Market
Context -> context-enrichment -> cross-venue allocation chain below is built
through the same existing application services
(``MaterializeSharedOpportunitySet``, ``MaterializeTotalMarketContextSnapshot``,
``AttachTotalMarketContext``, ``MaterializeCrossVenueAllocation``) that
``tests/persistence/test_postgres_cross_venue_allocation.py`` already proves,
with a deterministic, isolated fixture identity so the test is reproducible
offline. It proves the mechanism -- CRYPTO_PAPER_TRIAL_EXECUTION and
KALSHI_PAPER_TRIAL_EXECUTION correctly drive the existing internal PAPER
engines from a genuine cross-venue allocation proposal, replay idempotently,
and never touch a genuine provider endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
    PostgresCrossVenueAllocationSource,
)
from apex.adapters.crypto_market_pulse_postgres import PostgresCryptoMarketPulseRepository
from apex.adapters.crypto_paper_postgres import (
    PostgresCryptoPaperRepository,
)
from apex.adapters.crypto_paper_postgres import (
    account_table as crypto_account_table,
)
from apex.adapters.crypto_paper_postgres import (
    capability_table as crypto_capability_table,
)
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.kalshi_operating_postgres import (
    capability_table as kalshi_capability_table,
)
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import (
    GENUINE_CRYPTO_PAPER_ACCOUNT_ID,
    GENUINE_KALSHI_PAPER_ACCOUNT_ID,
    GENUINE_PAPER_OPENING_CASH,
    ExistingCapabilityOperatingTaskExecutor,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.application.cross_venue_allocation import MaterializeCrossVenueAllocation
from apex.application.crypto_market_pulse import RegisterCryptoRefreshPlan
from apex.application.kalshi_operating import CreateKalshiPaperAccount
from apex.application.operating_cycle import (
    ExecuteOperatingCycle,
    RegisterOperatingCyclePlan,
    SetPaperActivation,
)
from apex.application.shared_opportunity import MaterializeSharedOpportunitySet
from apex.application.total_market_context import (
    AttachTotalMarketContext,
    MaterializeTotalMarketContextSnapshot,
)
from apex.domain.crypto_market_pulse import (
    CryptoPulseCheckpoint,
    CryptoPulseSnapshot,
    CryptoRefreshPlan,
    CryptoRefreshRun,
)
from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperCapability,
    PaperOrderState,
    ProviderCapabilityState,
)
from apex.domain.crypto_universe import (
    CatalogPaginationStatus,
    CryptoProductCapture,
    CryptoProductPageEvidence,
    CryptoUniverseSnapshot,
    PublicCryptoProduct,
)
from apex.domain.kalshi_operating import (
    KalshiCapability,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiPaperOrderState,
    KalshiRefreshRecord,
    KalshiSide,
)
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskStatus,
    OperatingTaskType,
    PaperActivationState,
    PaperContinuationHealthState,
    TaskExecution,
    compute_paper_continuation_health,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.total_market_context import (
    ContextComponent,
    ContextComponentType,
    ContextSourceBundle,
    UnavailableContextComponent,
)
from apex.domain.values import canonical_json_bytes, content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)
MARKET_TICKER = "PAPER-TRIAL-FIXTURE-MARKET"
EVALUATION_ID = "paper-trial-fixture-evaluation"


def _projection(*, paper_eligible: bool) -> SharedOpportunityProjection:
    value = SharedOpportunityProjection.create(
        projection_id="paper-trial-fixture-kalshi-projection",
        venue=SharedVenue.KALSHI,
        source_record_type="BinaryComplementEvaluation",
        source_record_id=EVALUATION_ID,
        source_canonical_hash=content_digest(("paper-trial-fixture", "source")),
        strategy_or_method_id="KALSHI_RELATED_MARKET_CONSISTENCY_V1",
        instrument_id=MARKET_TICKER,
        product_or_market_id=MARKET_TICKER,
        direction="BUY_YES",
        generated_at=NOW,
        valid_until=NOW + timedelta(hours=6),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=30,
        freshness_limit_seconds=3600,
        comparable_value="0.06",
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
        supporting_evidence_ids=("paper-trial-fixture-evidence",),
        source_warnings=("ISOLATED_FIXTURE_NOT_GENUINE_MARKET_EVIDENCE",),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("ISOLATED_RECURRING_PAPER_TRIAL_ACCEPTANCE",),
        source_details=(("yes_side", "BUY_YES"),),
    )
    from dataclasses import replace

    return replace(value, paper_eligible=paper_eligible)


class _SharedSource:
    def __init__(self, *, paper_eligible: bool) -> None:
        self._paper_eligible = paper_eligible

    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return ()

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return (_projection(paper_eligible=self._paper_eligible),)

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        return ()


class _ContextSource:
    def load(self, *, source_batch_id: str, as_of: datetime) -> ContextSourceBundle:
        equity = ContextComponent.create(
            component_id="paper-trial-fixture-equity-context",
            component_type=ContextComponentType.US_EQUITY_MARKET,
            source_observation_id="paper-trial-fixture-spy-bar",
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
            supporting_evidence_ids=("paper-trial-fixture-context-evidence",),
        )
        volatility = ContextComponent.create(
            component_id="paper-trial-fixture-volatility-context",
            component_type=ContextComponentType.MARKET_VOLATILITY,
            source_observation_id="paper-trial-fixture-volatility-bar",
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
            supporting_evidence_ids=("paper-trial-fixture-volatility-evidence",),
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


def _retain_paper_readiness(engine: sa.Engine, *, plan_created_at: datetime) -> None:
    crypto_capability = CryptoPaperCapability(
        capability_id="paper-trial-fixture-crypto-capability",
        contract_id=None,
        primary_state=ProviderCapabilityState.UNAVAILABLE,
        states=(ProviderCapabilityState.UNAVAILABLE,),
        credentials_requested=False,
        provider_requests=0,
        inspected_at=NOW,
        limitations=("NO_RETAINED_COMPATIBLE_SPOT_PRODUCT_CONTRACT",),
        canonical_hash="a" * 64,
    )
    kalshi_capability = KalshiCapability.inspect(
        capability_id="paper-trial-fixture-kalshi-capability",
        inspected_at=NOW,
        public_discovery=True,
        public_orderbook=True,
        public_rules=True,
    )
    with engine.begin() as connection:
        # Crypto capability is genuinely unavailable in this fixture (no
        # contract retained); only Kalshi's PAPER boundary is made ready so
        # the allocation layer produces a KALSHI proposal only.
        connection.execute(
            crypto_capability_table.insert().values(
                capability_id=crypto_capability.capability_id,
                contract_id=None,
                idempotency_key="paper-trial-fixture-crypto-capability-key",
                inspected_at=NOW,
                payload=canonical_json_bytes(crypto_capability).decode("utf-8"),
                canonical_hash=crypto_capability.canonical_hash,
                provider_requests=0,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )
        connection.execute(
            kalshi_capability_table.insert().values(
                capability_id=kalshi_capability.capability_id,
                idempotency_key="paper-trial-fixture-kalshi-capability-key",
                inspected_at=NOW,
                payload=canonical_json_bytes(kalshi_capability).decode("utf-8"),
                canonical_hash=kalshi_capability.canonical_hash,
                provider_requests=0,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )
    # Bootstrap the account through the exact same service, idempotency key,
    # and created_at the recurring task itself uses, so the task's own
    # idempotent bootstrap check (`state(...) is not None`) recognizes it as
    # already-created rather than a conflicting duplicate.
    CreateKalshiPaperAccount(PostgresKalshiOperatingRepository(engine)).execute(
        account_id=GENUINE_KALSHI_PAPER_ACCOUNT_ID,
        opening_cash=GENUINE_PAPER_OPENING_CASH,
        created_at=plan_created_at,
        idempotency_key=f"{GENUINE_KALSHI_PAPER_ACCOUNT_ID}-bootstrap",
    )


def _build_allocation(engine: sa.Engine, *, paper_eligible: bool) -> str:
    """Run the real Shared Opportunity -> Total Market Context ->
    context-enrichment -> cross-venue allocation pipeline and return the
    resulting decision_id."""

    shared = PostgresSharedOpportunityRepository(engine)
    shared_result = MaterializeSharedOpportunitySet(
        shared, _SharedSource(paper_eligible=paper_eligible)
    ).execute(
        opportunity_set_id="paper-trial-fixture-shared-set",
        crypto_pulse_id="paper-trial-fixture-pulse",
        kalshi_evaluation_ids=(EVALUATION_ID,),
        as_of=NOW,
        idempotency_key="paper-trial-fixture-shared-key",
    )
    context = PostgresTotalMarketContextRepository(engine)
    context_result = MaterializeTotalMarketContextSnapshot(context, _ContextSource()).execute(
        snapshot_id="paper-trial-fixture-context",
        source_batch_id="paper-trial-fixture-context-batch",
        as_of=NOW,
        idempotency_key="paper-trial-fixture-context-key",
    )
    enriched = AttachTotalMarketContext(context).execute(
        enriched_set_id="paper-trial-fixture-enriched",
        opportunity_set_id=shared_result.opportunity_set.opportunity_set_id,
        context_snapshot_id=context_result.snapshot.snapshot_id,
        attached_at=NOW,
        idempotency_key="paper-trial-fixture-enriched-key",
    )
    cross_venue = PostgresCrossVenueAllocationRepository(engine)
    allocation_result = MaterializeCrossVenueAllocation(
        cross_venue, PostgresCrossVenueAllocationSource(engine)
    ).execute(
        decision_id="paper-trial-fixture-allocation",
        enriched_set_id=enriched.enriched_set.enriched_set_id,
        decided_at=NOW,
        idempotency_key="paper-trial-fixture-allocation-key",
    )
    return allocation_result.decision.decision_id


def _plan(engine: sa.Engine, *, paper_eligible: bool = False) -> OperatingCyclePlan:
    """Register the fixture operating-cycle plan.

    ``paper_eligible`` mirrors the exact, already-modeled
    ``OperatingCyclePlan.paper_eligible`` toggle that
    ``evaluate_paper_eligibility``/``KalshiPaperEligibility.evaluate`` read as
    ``operating_cycle_enabled``. It defaults to False, matching the genuine
    default (the recurring PAPER trial capability exists in code but is not
    turned on for any plan yet); tests that need to observe a real order being
    constructed pass True explicitly.
    """

    repository = PostgresOperatingCycleRepository(engine)
    if not paper_eligible:
        plan, _ = RegisterOperatingCyclePlan(repository).execute(
            plan_id="paper-trial-fixture-plan",
            crypto_pulse_plan_id="paper-trial-fixture-crypto-pulse-plan",
            actor="fixture-operator",
            created_at=NOW - timedelta(days=1),
            idempotency_key="paper-trial-fixture-plan-register",
        )
        return plan
    from dataclasses import replace

    plan = replace(
        OperatingCyclePlan.create(
            plan_id="paper-trial-fixture-plan",
            crypto_pulse_plan_id="paper-trial-fixture-crypto-pulse-plan",
            actor="fixture-operator",
            created_at=NOW - timedelta(days=1),
        ),
        paper_eligible=True,
    )
    repository.save_plan(idempotency_key="paper-trial-fixture-plan-register", plan=plan)
    return plan


def _due_task(task_type: OperatingTaskType, *, cycle_id: str) -> DueTask:
    return DueTask.create(
        task_type=task_type,
        disposition=DueDisposition.DUE,
        reasons=("FIXTURE_ACCEPTANCE",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_WRITES",
    )


def _executor(engine: sa.Engine) -> ExistingCapabilityOperatingTaskExecutor:
    state_source = PostgresOperatingStateSource(engine, PostgresOperatingCycleRepository(engine))
    return ExistingCapabilityOperatingTaskExecutor(engine, state_source=state_source)


def test_kalshi_paper_trial_executes_reconciles_and_replays_idempotently(
    postgres_engine: sa.Engine,
) -> None:
    plan = _plan(postgres_engine, paper_eligible=True)
    _retain_paper_readiness(postgres_engine, plan_created_at=plan.created_at)
    _build_allocation(postgres_engine, paper_eligible=True)

    kalshi = PostgresKalshiOperatingRepository(postgres_engine)
    opportunity = KalshiCurrentOpportunity.create(
        opportunity_id="paper-trial-fixture-opportunity-yes",
        refresh_id="paper-trial-fixture-refresh",
        evaluation_id=EVALUATION_ID,
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_ticker=MARKET_TICKER,
        event_ticker="PAPER-TRIAL-FIXTURE-EVENT",
        rule_hash="a" * 64,
        side=KalshiSide.YES,
        price=Decimal("0.45"),
        visible_liquidity=100,
        close_at=NOW + timedelta(hours=2),
        settlement_at=NOW + timedelta(hours=3),
        evidence_observed_at=NOW - timedelta(minutes=1),
        as_of=NOW,
        supporting_evidence_ids=("fixture-market", "fixture-orderbook", "fixture-fee"),
    )
    refresh = KalshiRefreshRecord.create(
        refresh_id="paper-trial-fixture-refresh",
        as_of=NOW,
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_discovery_run_id="paper-trial-fixture-discovery",
        market_configuration_hash="l" * 64,
        complement_configuration_hash="m" * 64,
        page_count=1,
        market_count=1,
        markets_evaluated=1,
        opportunities=(opportunity,),
        evaluation_ids=(opportunity.evaluation_id,),
        provider_requests=0,
        request_bounds=(),
        checkpoints=(),
        warnings=(),
        completed_at=NOW,
    )
    kalshi.save_refresh(
        refresh=refresh, opportunities=(opportunity,), idempotency_key="paper-trial-fixture-refresh"
    )

    executor = _executor(postgres_engine)
    task = _due_task(
        OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION, cycle_id="paper-trial-fixture-cycle"
    )
    first = executor.execute(
        task=task, cycle_id="paper-trial-fixture-cycle", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert first.status is OperatingTaskStatus.COMPLETED
    assert "KALSHI_PAPER_ELIGIBLE:1" in first.warnings
    assert "KALSHI_PAPER_ORDERS_CREATED:1" in first.warnings
    assert not any(code.startswith("KALSHI_PAPER_REJECTED:") for code in first.warnings)
    assert "NO_KALSHI_ORDER_SUBMISSION" in first.warnings

    account = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
    assert account is not None
    assert len(account.orders) == 1
    assert account.orders[0].market_ticker == MARKET_TICKER
    assert account.reconciliations[-1].status.value == "CLEAN"

    replay = executor.execute(
        task=task, cycle_id="paper-trial-fixture-cycle-2", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert replay.status in {OperatingTaskStatus.COMPLETED, OperatingTaskStatus.REUSED}
    reconciled = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
    assert reconciled is not None
    assert len(reconciled.orders) == 1, "replay must not create a duplicate order"
    assert len(reconciled.authorizations) == 1, "replay must not create a duplicate authorization"


def test_crypto_paper_trial_honestly_rejects_when_projection_not_paper_eligible(
    postgres_engine: sa.Engine,
) -> None:
    """A crypto SharedOpportunityProjection not marked paper_eligible (whether
    because it fails the canonical construction-time policy in
    ``SharedOpportunityProjection.create`` or, as forced here via the fixture
    source's own ``paper_eligible`` override, for isolated determinism) must
    make the recurring task report that fact honestly (a real, named blocker)
    instead of fabricating a PAPER order.

    (Kalshi is used as the venue under test here purely because it is cheaper
    to seed genuinely through the real allocation pipeline than a full crypto
    universe/pulse chain. Kalshi's own PAPER eligibility never reads this
    field -- see ``KalshiPaperEligibility.evaluate``'s ``shared_eligible``
    derivation in domain/kalshi_operating.py -- so this fixture forces
    ``paper_eligible=False`` on the KALSHI-venue projection purely to exercise
    the identically-shaped rejection reporting path; crypto's own
    ``projection.paper_eligible`` gate is proven directly in
    domain/crypto_paper.py's and tests/test_shared_opportunity.py's tests.)
    """

    plan = _plan(postgres_engine, paper_eligible=True)
    _retain_paper_readiness(postgres_engine, plan_created_at=plan.created_at)
    _build_allocation(postgres_engine, paper_eligible=False)

    executor = _executor(postgres_engine)
    task = _due_task(
        OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION, cycle_id="paper-trial-fixture-cycle"
    )
    result = executor.execute(
        task=task, cycle_id="paper-trial-fixture-cycle", plan=plan, as_of=NOW, executed_at=NOW
    )
    # COMPLETED, not REUSED: a fresh reconciliation record is genuinely
    # materialized every cycle even when zero orders are created.
    assert result.status is OperatingTaskStatus.COMPLETED
    assert "KALSHI_PAPER_ORDERS_CREATED:0" in result.warnings
    paper = PostgresCryptoPaperRepository(postgres_engine)
    assert paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID) is None


def test_kalshi_paper_trial_reports_no_action_without_allocation(
    postgres_engine: sa.Engine,
) -> None:
    plan = _plan(postgres_engine)
    executor = _executor(postgres_engine)
    task = _due_task(
        OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION, cycle_id="paper-trial-no-alloc"
    )
    with pytest.raises(Exception, match="OPERATING_KALSHI_PAPER_ALLOCATION_MISSING"):
        executor.execute(
            task=task, cycle_id="paper-trial-no-alloc", plan=plan, as_of=NOW, executed_at=NOW
        )


# --------------------------------------------------------------------------
# KALSHI_PAPER_TRIAL_EXECUTION: intraday continuation through the REAL
# ExecuteOperatingCycle scheduler (CRYPTO/KALSHI PAPER Intraday Lifecycle
# Recovery v1). Every test above calls
# ``ExistingCapabilityOperatingTaskExecutor.execute()`` DIRECTLY with a
# distinct cycle_id per call -- real proof of the executor's own domain
# idempotency, but it cannot observe whether the real scheduler would ever
# dispatch this task type twice under one genuine, unchanged cycle_id
# (production's actual shape, per cli.py: ``cycle_id =
# f"{plan_id}-worker-{YYYYMMDD}"``, pinned all UTC day). These tests close
# exactly that gap, mirroring test_postgres_strategy_paper_trial_recurring.
# py's own ``TestRealSchedulerIntradayContinuation``.
# --------------------------------------------------------------------------


def _activate_paper(engine: sa.Engine, *, plan_id: str, created_at: datetime) -> None:
    """Author a genuine ``PaperActivationEvent`` enabling PAPER
    consideration for ``plan_id`` -- required for the REAL
    ``ExecuteOperatingCycle`` scheduler path (``effective_paper_activation``
    never defaults to enabled), unlike a DIRECT ``executor.execute()`` call
    against a raw ``_plan(..., paper_eligible=True)`` object (which bakes
    the flag onto the plan row itself -- never read by the real scheduler,
    which instead folds it from this event history)."""

    SetPaperActivation(PostgresOperatingCycleRepository(engine)).execute(
        event_id=f"{plan_id}-paper-activation",
        plan_id=plan_id,
        state=PaperActivationState.ENABLED,
        reason="real-scheduler-paper-trial-test",
        actor="test:real-scheduler-paper-trial",
        occurred_at=created_at,
        idempotency_key=f"{plan_id}-paper-activation-key",
    )


def _seed_minimal_crypto_pulse_prerequisite(
    engine: sa.Engine, *, crypto_pulse_plan_id: str, as_of: datetime
) -> None:
    """``SHARED_OPPORTUNITY_REBUILD``'s own planning-time due-check
    (``domain.operating_cycle.plan_due_work``) is unconditionally
    ``BLOCKED``/``CRYPTO_PULSE_REQUIRED`` whenever ``retained.crypto_pulse_
    id is None`` -- true regardless of which venue's proposal is actually
    under test, and cascades to ``BLOCKED``/``DEPENDENCY_FAILED`` on
    ``CONTEXT_ENRICHMENT_REBUILD`` -> ``RESEARCH_ALLOCATION_REBUILD`` ->
    both PAPER trial tasks. The hand-crafted ``DueTask`` the executor-
    direct tests above feed straight to ``executor.execute()`` bypasses
    this planning layer entirely (discovered empirically: a real-scheduler
    KALSHI-only test failed ``BLOCKED``/``DEPENDENCY_FAILED`` with zero
    orders until this was added) -- the REAL ``ExecuteOperatingCycle``
    scheduler does not, so even a KALSHI-only scheduler-level test needs a
    minimal genuine crypto pulse to exist, purely to satisfy this
    precondition. Unrelated to the CRYPTO venue's own PAPER-boundary
    readiness gate (``_retain_crypto_paper_readiness``), which stays
    correctly absent/UNAVAILABLE for a KALSHI-only test -- this seeds only
    enough for ``state.crypto_pulse_id`` to be non-``None``, not a genuine
    CRYPTO proposal."""

    market_pulse = PostgresCryptoMarketPulseRepository(engine)
    universe, pulse_plan = _seed_crypto_universe_and_plan(
        market_pulse,
        product_id=CRYPTO_PRODUCT_ID,
        plan_id=crypto_pulse_plan_id,
        created_at=as_of,
    )
    _open_crypto_pulse(
        market_pulse,
        plan=pulse_plan,
        universe=universe,
        product_id=CRYPTO_PRODUCT_ID,
        canonical_asset_id="BTC",
        price=Decimal("50000"),
        quote_captured_at=as_of - timedelta(minutes=1),
        as_of=as_of,
    )


def _save_kalshi_refresh(
    kalshi: PostgresKalshiOperatingRepository,
    *,
    refresh_id: str,
    as_of: datetime,
    price: Decimal,
    visible_liquidity: int,
    evidence_observed_at: datetime,
) -> KalshiCurrentOpportunity:
    """Save a fresh KALSHI refresh + single YES opportunity on
    ``MARKET_TICKER``/``EVALUATION_ID`` -- the same fixture shape
    ``test_kalshi_paper_trial_executes_reconciles_and_replays_idempotently``
    above builds inline, parametrized so a later-tick test can seed a
    genuinely newer/different refresh
    (``PostgresKalshiOperatingRepository.latest_refresh`` orders by
    ``as_of DESC``)."""

    opportunity = KalshiCurrentOpportunity.create(
        opportunity_id=f"{refresh_id}-opportunity-yes",
        refresh_id=refresh_id,
        evaluation_id=EVALUATION_ID,
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_ticker=MARKET_TICKER,
        event_ticker="PAPER-TRIAL-FIXTURE-EVENT",
        rule_hash="a" * 64,
        side=KalshiSide.YES,
        price=price,
        visible_liquidity=visible_liquidity,
        close_at=as_of + timedelta(hours=2),
        settlement_at=as_of + timedelta(hours=3),
        evidence_observed_at=evidence_observed_at,
        as_of=as_of,
        supporting_evidence_ids=("fixture-market", "fixture-orderbook", "fixture-fee"),
    )
    refresh = KalshiRefreshRecord.create(
        refresh_id=refresh_id,
        as_of=as_of,
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_discovery_run_id=f"{refresh_id}-discovery",
        market_configuration_hash="l" * 64,
        complement_configuration_hash="m" * 64,
        page_count=1,
        market_count=1,
        markets_evaluated=1,
        opportunities=(opportunity,),
        evaluation_ids=(opportunity.evaluation_id,),
        provider_requests=0,
        request_bounds=(),
        checkpoints=(),
        warnings=(),
        completed_at=as_of,
    )
    kalshi.save_refresh(refresh=refresh, opportunities=(opportunity,), idempotency_key=refresh_id)
    return opportunity


class _RealPaperExecutorWrapper:
    """Delegates exactly one task type to the real, Postgres-backed
    ``ExistingCapabilityOperatingTaskExecutor``; every other task type
    completes trivially. Mirrors ``_RealStrategyPaperExecutorWrapper`` in
    tests/persistence/test_postgres_strategy_paper_trial_recurring.py (kept
    as its own small copy rather than imported across test modules,
    matching this file's existing convention of self-contained fixtures) --
    proves the real CRYPTO/KALSHI PAPER trial orchestration through the
    real ``ExecuteOperatingCycle`` scheduler without also standing up every
    other task's unrelated fixtures. Every genuine (non-trivial) dispatch's
    raw ``TaskExecution`` is retained in ``results`` so a test can inspect
    what the real executor branch did on a call the scheduler chose not to
    persist as a terminal ``OperatingTaskResult`` (see ``TaskExecution.
    awaits_intraday_continuation``)."""

    def __init__(
        self, real: ExistingCapabilityOperatingTaskExecutor, *, task_type: OperatingTaskType
    ) -> None:
        self._real = real
        self._task_type = task_type
        self.calls: list[tuple[str, datetime]] = []
        self.results: list[TaskExecution] = []

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        self.calls.append((task.task_type.value, executed_at))
        if task.task_type is self._task_type:
            result = self._real.execute(
                task=task, cycle_id=cycle_id, plan=plan, as_of=as_of, executed_at=executed_at
            )
            self.results.append(result)
            return result
        return TaskExecution(
            OperatingTaskStatus.COMPLETED, (f"{task.task_type.value}-output",), (), True, 0
        )


class TestKalshiRealSchedulerIntradayContinuation:
    def test_same_cycle_id_later_tick_genuinely_redispatches_and_fills(
        self, postgres_engine: sa.Engine
    ) -> None:
        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_paper_readiness(postgres_engine, plan_created_at=plan.created_at)
        _build_allocation(postgres_engine, paper_eligible=True)
        _seed_minimal_crypto_pulse_prerequisite(
            postgres_engine, crypto_pulse_plan_id=plan.crypto_pulse_plan_id, as_of=NOW
        )
        kalshi = PostgresKalshiOperatingRepository(postgres_engine)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="paper-trial-fixture-refresh-open",
            as_of=NOW,
            price=Decimal("0.45"),
            visible_liquidity=100,
            evidence_observed_at=NOW - timedelta(minutes=1),
        )
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id = "paper-trial-fixture-plan-worker-scheduler"

        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        opened = ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "KALSHI_PAPER_TRIAL_EXECUTION" not in {item.task_type.value for item in opened.tasks}
        assert len(opened_wrapper.results) == 1
        assert opened_wrapper.results[0].awaits_intraday_continuation is True
        account = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert account is not None
        order_id = account.orders[0].order_id

        # Same cycle_id, later executed_at -- a genuinely new
        # ExecuteOperatingCycle/executor pair (simulated worker restart)
        # over the same repository, with genuinely fresher evidence seeded
        # (a new, later KALSHI refresh -- this task's fill evidence is
        # KALSHI's own current-refresh mechanism, not LiveMarketObservation).
        later = NOW + timedelta(minutes=20)
        fill_opportunity = _save_kalshi_refresh(
            kalshi,
            refresh_id="paper-trial-fixture-refresh-fill",
            as_of=later,
            price=Decimal("0.45"),
            visible_liquidity=1000,
            evidence_observed_at=later,
        )
        fill_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, fill_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=later + timedelta(minutes=1),
            idempotency_key=f"{cycle_id}-key",
        )
        # The core regression proof: the real executor branch was genuinely
        # called a second time under the identical cycle_id the real worker
        # actually reuses all UTC day -- pre-fix, ``_resume_cycle`` would
        # find KALSHI_PAPER_TRIAL_EXECUTION already persisted from the
        # ``opened`` tick above and skip it without ever reaching the
        # executor, so ``fill_wrapper.results`` would stay empty here.
        assert len(fill_wrapper.results) == 1
        assert fill_wrapper.results[0].awaits_intraday_continuation is True
        assert "KALSHI_PAPER_FILLS_APPLIED:1" in fill_wrapper.results[0].warnings

        state = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate order from the later tick
        filled_order = next(item for item in state.orders if item.order_id == order_id)
        assert filled_order.state is KalshiPaperOrderState.FILLED
        fill_row = next(item for item in state.fills if item.order_id == order_id)
        assert fill_opportunity.opportunity_id in fill_row.evidence_id

    def test_same_cycle_id_later_tick_expires_without_duplicating(
        self, postgres_engine: sa.Engine
    ) -> None:
        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_paper_readiness(postgres_engine, plan_created_at=plan.created_at)
        _build_allocation(postgres_engine, paper_eligible=True)
        _seed_minimal_crypto_pulse_prerequisite(
            postgres_engine, crypto_pulse_plan_id=plan.crypto_pulse_plan_id, as_of=NOW
        )
        kalshi = PostgresKalshiOperatingRepository(postgres_engine)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="paper-trial-fixture-refresh-open",
            as_of=NOW,
            price=Decimal("0.45"),
            visible_liquidity=100,
            evidence_observed_at=NOW - timedelta(minutes=1),
        )
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id = "paper-trial-fixture-plan-worker-scheduler-expiry"

        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id}-key",
        )
        account = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert account is not None
        order_id = account.orders[0].order_id

        # Same cycle_id, past the ~1-hour order validity window -- the
        # AUTHORIZATION's own expiry (``min(proposal.expires_at, executed_at
        # + timedelta(hours=1))``, propagated preview -> order verbatim; see
        # domain/kalshi_operating.py's ``preview_paper_order``/
        # ``submit_paper_order``). ``KALSHI_ORDER_VALID_FOR`` in
        # operating_cycle_tasks.py is dead code -- grepped and confirmed
        # unused anywhere else in src/ -- so KALSHI's real order-validity
        # window is genuinely ~1 hour, not 4 like CRYPTO's. Must still
        # genuinely redispatch, recognize the already-open order without
        # duplicating it, and expire it on THIS tick -- not 24 hours later
        # at the next UTC day's own first tick.
        much_later = NOW + timedelta(minutes=75)
        expiry_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, expiry_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=much_later,
            idempotency_key=f"{cycle_id}-key",
        )
        assert len(expiry_wrapper.results) == 1
        assert expiry_wrapper.results[0].awaits_intraday_continuation is True
        assert "KALSHI_PAPER_ORDERS_EXPIRED:1" in expiry_wrapper.results[0].warnings

        state = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate order from the later tick
        expired_order = next(item for item in state.orders if item.order_id == order_id)
        assert expired_order.state is KalshiPaperOrderState.EXPIRED

    def test_same_cycle_id_second_genuine_fill_is_not_silently_lost(
        self, postgres_engine: sa.Engine
    ) -> None:
        """Phase C: ``fill_id``/its ``idempotency_key`` were BOTH derived
        only from ``(order.order_id, cycle_id)`` -- harmless while this task
        genuinely dispatched at most once per cycle_id, but once a cycle_id
        can span many genuine dispatches (Phase B), a SECOND genuine fill on
        the SAME still-open order within the SAME cycle_id reused the
        FIRST fill's identical fill_id/idempotency_key.
        ``ApplyKalshiPaperFill`` then treated the second attempt as a replay
        of the first (matching retained content) rather than a genuinely
        new fill -- the second fill was silently discarded, not rejected
        loudly, and the order could never progress past its first partial
        fill within one UTC day."""

        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_paper_readiness(postgres_engine, plan_created_at=plan.created_at)
        _build_allocation(postgres_engine, paper_eligible=True)
        _seed_minimal_crypto_pulse_prerequisite(
            postgres_engine, crypto_pulse_plan_id=plan.crypto_pulse_plan_id, as_of=NOW
        )
        kalshi = PostgresKalshiOperatingRepository(postgres_engine)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="paper-trial-fixture-refresh-open",
            as_of=NOW,
            price=Decimal("0.45"),
            visible_liquidity=100,
            evidence_observed_at=NOW - timedelta(minutes=1),
        )
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id = "paper-trial-fixture-plan-worker-scheduler-double-fill"

        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id}-key",
        )
        account = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert account is not None
        order = account.orders[0]
        order_id = order.order_id
        total_contracts = order.contracts
        # Otherwise this scenario cannot distinguish a partial fill from a
        # full one (genuinely 52 contracts as of this fixture's sizing).
        assert total_contracts > 1

        # Tick 2: same cycle_id, fresh evidence, but visible liquidity
        # covers only ONE contract -- a genuine PARTIAL fill.
        partial_at = NOW + timedelta(minutes=15)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="paper-trial-fixture-refresh-partial",
            as_of=partial_at,
            price=Decimal("0.45"),
            visible_liquidity=1,
            evidence_observed_at=partial_at,
        )
        partial_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, partial_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=partial_at + timedelta(seconds=1),
            idempotency_key=f"{cycle_id}-key",
        )
        assert "KALSHI_PAPER_FILLS_APPLIED:1" in partial_wrapper.results[0].warnings
        after_partial = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert after_partial is not None
        partially_filled = next(item for item in after_partial.orders if item.order_id == order_id)
        assert partially_filled.filled_contracts == 1
        assert partially_filled.state is KalshiPaperOrderState.PARTIALLY_FILLED

        # Tick 3: SAME cycle_id, still well within the ~1h validity window,
        # fresh evidence with ample liquidity for the remainder -- the real,
        # demonstrated redispatch conflict under test: does this second
        # genuine fill actually get recorded, or does it silently replay
        # the first (stale ``fill_id``/idempotency_key collision)?
        remainder_at = NOW + timedelta(minutes=30)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="paper-trial-fixture-refresh-remainder",
            as_of=remainder_at,
            price=Decimal("0.45"),
            visible_liquidity=1000,
            evidence_observed_at=remainder_at,
        )
        remainder_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, remainder_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=remainder_at + timedelta(seconds=1),
            idempotency_key=f"{cycle_id}-key",
        )

        final = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert final is not None
        final_order = next(item for item in final.orders if item.order_id == order_id)
        assert final_order.filled_contracts == total_contracts
        assert final_order.state is KalshiPaperOrderState.FILLED
        fills_for_order = [item for item in final.fills if item.order_id == order_id]
        assert len(fills_for_order) == 2, (
            "the second genuine fill must not silently replay the first"
        )


# --------------------------------------------------------------------------
# CRYPTO_PAPER_TRIAL_EXECUTION: intraday continuation through the REAL
# ExecuteOperatingCycle scheduler (CRYPTO/KALSHI PAPER Intraday Lifecycle
# Recovery v1). The CRYPTO counterpart of ``TestKalshiRealSchedulerIntraday
# Continuation`` above -- a distinct fixture chain is needed because the
# existing crypto coverage in this file (``test_crypto_paper_trial_honestly_
# rejects_when_projection_not_paper_eligible``) deliberately reuses Kalshi
# fixtures and never builds a genuine CRYPTO-venue proposal. This one does,
# mirroring test_postgres_cross_venue_allocation.py's own crypto-venue
# readiness technique (raw-inserted, disposable ``crypto_capability_table``/
# ``crypto_account_table`` rows -- structurally unrelated to
# ``GENUINE_CRYPTO_PAPER_ACCOUNT_ID``, which ``_execute_crypto_paper_trial``
# bootstraps itself) plus a minimal, hand-constructed universe/pulse chain
# (bypassing ``materialize_universe``/``build_pulse_snapshot``'s much
# heavier history-depth/quality requirements, which neither
# ``_crypto_contract`` nor ``evaluate_paper_eligibility`` nor
# ``_crypto_price_evidence`` actually need).
# --------------------------------------------------------------------------

CRYPTO_PRODUCT_ID = "BTC-USD"


def _crypto_projection(*, paper_eligible: bool) -> SharedOpportunityProjection:
    value = SharedOpportunityProjection.create(
        projection_id="paper-trial-fixture-crypto-projection",
        venue=SharedVenue.CRYPTO,
        source_record_type="CryptoFixtureOpportunity",
        source_record_id="paper-trial-fixture-crypto-source",
        source_canonical_hash=content_digest(("paper-trial-fixture-crypto", "source")),
        strategy_or_method_id="CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
        instrument_id=CRYPTO_PRODUCT_ID,
        product_or_market_id=CRYPTO_PRODUCT_ID,
        direction="BUY",
        generated_at=NOW,
        valid_until=NOW + timedelta(hours=6),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=30,
        freshness_limit_seconds=3600,
        comparable_value="0.08",
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
        supporting_evidence_ids=("paper-trial-fixture-crypto-evidence",),
        source_warnings=("ISOLATED_FIXTURE_NOT_GENUINE_MARKET_EVIDENCE",),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("ISOLATED_RECURRING_PAPER_TRIAL_ACCEPTANCE",),
        source_details=(("side", "BUY"),),
    )
    from dataclasses import replace

    return replace(value, paper_eligible=paper_eligible)


class _CryptoSharedSource:
    def __init__(self, *, paper_eligible: bool) -> None:
        self._paper_eligible = paper_eligible

    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return (_crypto_projection(paper_eligible=self._paper_eligible),)

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return ()

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        return ()


def _retain_crypto_paper_readiness(engine: sa.Engine) -> None:
    """The CRYPTO-venue counterpart of ``test_postgres_cross_venue_
    allocation.py``'s own ``_retain_paper_readiness``: a disposable
    capability/account pair that only satisfies ``allocate_cross_venue``'s
    readiness gate -- structurally unrelated to
    ``GENUINE_CRYPTO_PAPER_ACCOUNT_ID``/its own contract, which
    ``_execute_crypto_paper_trial`` bootstraps independently."""

    crypto_capability = CryptoPaperCapability(
        capability_id="paper-trial-fixture-crypto-readiness-capability",
        contract_id=None,
        primary_state=ProviderCapabilityState.PAPER_ONLY,
        states=(ProviderCapabilityState.PAPER_ONLY,),
        credentials_requested=False,
        provider_requests=0,
        inspected_at=NOW,
        limitations=("FIXTURE_ONLY",),
        canonical_hash="p" * 64,
    )
    crypto_account = CryptoPaperAccount.create(
        account_id="paper-trial-fixture-crypto-readiness-account",
        opening_cash="1000",
        created_at=NOW,
    )
    with engine.begin() as connection:
        connection.execute(
            crypto_capability_table.insert().values(
                capability_id=crypto_capability.capability_id,
                contract_id=None,
                idempotency_key="paper-trial-fixture-crypto-readiness-capability-key",
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
                idempotency_key="paper-trial-fixture-crypto-readiness-account-key",
                payload=canonical_json_bytes(crypto_account).decode("utf-8"),
                canonical_hash=crypto_account.canonical_hash,
                authority="PAPER_ONLY",
                live_authority="NONE",
            )
        )


def _build_crypto_allocation(engine: sa.Engine, *, paper_eligible: bool) -> str:
    """The CRYPTO-venue counterpart of ``_build_allocation`` above -- same
    real Shared Opportunity -> Total Market Context -> context-enrichment ->
    cross-venue allocation pipeline, seeded with a genuine CRYPTO-venue
    projection instead of KALSHI's."""

    shared = PostgresSharedOpportunityRepository(engine)
    shared_result = MaterializeSharedOpportunitySet(
        shared, _CryptoSharedSource(paper_eligible=paper_eligible)
    ).execute(
        opportunity_set_id="paper-trial-fixture-crypto-shared-set",
        crypto_pulse_id="paper-trial-fixture-crypto-pulse-for-shared",
        kalshi_evaluation_ids=(),
        as_of=NOW,
        idempotency_key="paper-trial-fixture-crypto-shared-key",
    )
    context = PostgresTotalMarketContextRepository(engine)
    context_result = MaterializeTotalMarketContextSnapshot(context, _ContextSource()).execute(
        snapshot_id="paper-trial-fixture-crypto-context",
        source_batch_id="paper-trial-fixture-crypto-context-batch",
        as_of=NOW,
        idempotency_key="paper-trial-fixture-crypto-context-key",
    )
    enriched = AttachTotalMarketContext(context).execute(
        enriched_set_id="paper-trial-fixture-crypto-enriched",
        opportunity_set_id=shared_result.opportunity_set.opportunity_set_id,
        context_snapshot_id=context_result.snapshot.snapshot_id,
        attached_at=NOW,
        idempotency_key="paper-trial-fixture-crypto-enriched-key",
    )
    cross_venue = PostgresCrossVenueAllocationRepository(engine)
    allocation_result = MaterializeCrossVenueAllocation(
        cross_venue, PostgresCrossVenueAllocationSource(engine)
    ).execute(
        decision_id="paper-trial-fixture-crypto-allocation",
        enriched_set_id=enriched.enriched_set.enriched_set_id,
        decided_at=NOW,
        idempotency_key="paper-trial-fixture-crypto-allocation-key",
    )
    return allocation_result.decision.decision_id


def _seed_crypto_universe_and_plan(
    market_pulse: PostgresCryptoMarketPulseRepository,
    *,
    product_id: str,
    plan_id: str,
    created_at: datetime,
) -> tuple[CryptoUniverseSnapshot, CryptoRefreshPlan]:
    """Minimal universe + refresh-plan chain -- enough for ``_crypto_
    contract``/``evaluate_paper_eligibility`` (product routing, eligible-
    product-id membership) without ``materialize_universe``'s much heavier
    170-bucket history-depth requirement, which this task's own execution
    path never reads (Phase A archaeology, confirmed by direct source
    citation in this milestone's report)."""

    product = PublicCryptoProduct.create(
        product_id=product_id,
        provider="COINBASE_ADVANCED_TRADE",
        base_asset=product_id.split("-")[0],
        quote_asset=product_id.split("-")[1],
        market_type="SPOT",
        trading_status="ACTIVE",
        product_status="ONLINE",
        price_increment="0.01",
        quantity_increment="0.00000001",
        minimum_quantity="0.00000001",
        minimum_notional="1",
        public_volume_24h="1000000",
        captured_at=created_at,
        source_contract_version="COINBASE_PUBLIC_PRODUCTS_CURSOR_V1",
        raw_evidence_hash="a" * 64,
        parser_version="CRYPTO_PRODUCT_PARSER_V1",
        evidence_class=EvidenceClass.FIXTURE,
    )
    page = CryptoProductPageEvidence.create(
        page_ordinal=1,
        request_cursor=None,
        next_cursor=None,
        has_next=False,
        product_ids=(product_id,),
        raw_evidence_hash="b" * 64,
        raw_bytes=1,
        completed_at=created_at,
        source_locator="fixture://products",
        runtime_milliseconds=1,
    )
    capture = CryptoProductCapture.create(
        capture_id=f"{plan_id}-capture",
        provider="COINBASE_ADVANCED_TRADE",
        captured_at=created_at,
        source_contract_version="COINBASE_PUBLIC_PRODUCTS_CURSOR_V1",
        parser_version="CRYPTO_PRODUCT_PARSER_V1",
        evidence_class=EvidenceClass.FIXTURE,
        raw_evidence_hash="c" * 64,
        products=(product,),
        request_count=1,
        limitations=(),
        pages=(page,),
        pagination_status=CatalogPaginationStatus.COMPLETE,
        continuation_cursor=None,
        total_raw_bytes=1,
        runtime_milliseconds=1,
    )
    market_pulse.save_product_capture(capture, products=(product,))
    universe = CryptoUniverseSnapshot(
        snapshot_id=f"{plan_id}-universe",
        source_capture_id=capture.capture_id,
        policy_id="fixture-universe-policy",
        policy_version=1,
        considered_product_ids=(product_id,),
        decisions=(),
        eligible_product_ids=(product_id,),
        excluded_product_ids=(),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_limitations=(),
        manifest_hash=f"{plan_id}-universe-manifest",
        created_at=created_at,
        canonical_hash="d" * 64,
    )
    market_pulse.save_universe_snapshot(universe)
    plan = RegisterCryptoRefreshPlan(market_pulse).execute(
        plan_id=plan_id,
        universe_snapshot_id=universe.snapshot_id,
        actor="test:real-scheduler-crypto-paper",
        created_at=created_at,
    )
    return universe, plan


def _open_crypto_pulse(
    market_pulse: PostgresCryptoMarketPulseRepository,
    *,
    plan: CryptoRefreshPlan,
    universe: CryptoUniverseSnapshot,
    product_id: str,
    canonical_asset_id: str,
    price: Decimal,
    quote_captured_at: datetime,
    as_of: datetime,
    refresh_id: str | None = None,
) -> CryptoRefreshRun:
    """Create the one ``CryptoRefreshRun`` a plan genuinely gets per UTC day
    + its one-and-only quote-populated ``CryptoPulseCheckpoint`` per product
    + a ``CryptoPulseSnapshot``. Both bounds are real, schema-level
    invariants discovered empirically while writing this fixture, not just
    due-mapping conventions: ``CryptoRefreshRun.create`` computes
    ``completed_utc_day(as_of)`` and ``PostgresCryptoMarketPulseRepository.
    save_refresh_run`` genuinely conflicts (``CRYPTO_PULSE_PLAN_AS_OF_
    CONFLICT``) on a second same-(plan, UTC day) call; ``crypto_market_
    pulse_checkpoint`` carries a real ``UNIQUE (refresh_id, product_id)``
    constraint. See ``TestCryptoRealSchedulerIntradayContinuation``'s own
    docstring below for what this means for CRYPTO's genuinely achievable
    same-day PAPER lifecycle.

    ``refresh_id`` defaults to the original, single-day-per-test
    ``f"{plan.plan_id}-refresh"`` every existing caller in this file already
    relies on -- pass an explicit distinct value (as
    ``TestCryptoContinuationHealthSignal`` does for its genuinely-second
    real UTC day) to open a second refresh under the same plan without
    colliding with the first day's now-immutable, differently-hashed
    ``CryptoRefreshRun`` row of that same default id."""

    refresh = CryptoRefreshRun.create(
        refresh_id=refresh_id or f"{plan.plan_id}-refresh",
        plan=plan,
        universe=universe,
        as_of=as_of,
        created_at=as_of,
    )
    market_pulse.save_refresh_run(refresh)
    spread = price * Decimal("0.0005")
    checkpoint = CryptoPulseCheckpoint.create(
        refresh_id=refresh.refresh_id,
        ordinal=1,
        canonical_asset_id=canonical_asset_id,
        product_id=product_id,
        product_status="ONLINE",
        status_observed_at=quote_captured_at,
        observation=None,
        as_of=as_of,
        raw_bytes=1,
        runtime_milliseconds=1,
        source_locator="fixture://quote",
        best_bid=price - spread,
        best_ask=price + spread,
        midpoint=price,
        spread_bps=Decimal("10"),
        visible_bid_depth=Decimal("10"),
        visible_ask_depth=Decimal("10"),
        quote_captured_at=quote_captured_at,
        quote_raw_evidence_hash="e" * 64,
    )
    market_pulse.save_pulse_checkpoint(checkpoint)
    pulse = CryptoPulseSnapshot(
        # Derived from refresh.refresh_id (not plan.plan_id directly) so a
        # caller opening a second, genuinely distinct refresh under the same
        # plan (a new UTC day) gets its own distinct pulse_id/manifest_hash
        # too -- no existing test in this file asserts either exact string.
        pulse_id=f"{refresh.refresh_id}-pulse",
        refresh_id=refresh.refresh_id,
        universe_snapshot_id=universe.snapshot_id,
        as_of=as_of,
        eligible_assets=(canonical_asset_id,),
        selected_routes=((canonical_asset_id, product_id),),
        checkpoint_ids=(checkpoint.checkpoint_id,),
        observation_ids=(),
        event_time=as_of,
        ingested_at=as_of,
        maximum_freshness_seconds=0,
        missing_observation_count=0,
        current_statuses=((product_id, "ONLINE"),),
        # manifest_hash is VARCHAR(64) with its own UNIQUE constraint --
        # unlike pulse_id/refresh_id (VARCHAR(160)), an id-derived string
        # can overflow it once refresh_id is no longer the short, fixed
        # single-day default, so this is a genuine (deterministic, still
        # collision-free per distinct refresh_id) hash rather than a
        # human-readable suffix.
        manifest_hash=content_digest((refresh.refresh_id, "pulse-manifest")),
        limitations=(),
        canonical_hash="f" * 64,
    )
    market_pulse.save_pulse_snapshot(pulse)
    return refresh


class TestCryptoRealSchedulerIntradayContinuation:
    def test_same_cycle_id_later_tick_genuinely_redispatches_and_reconciles(
        self, postgres_engine: sa.Engine
    ) -> None:
        """No genuine same-day FILL variant exists for CRYPTO (unlike
        KALSHI's ``test_same_cycle_id_later_tick_genuinely_redispatches_and_
        fills`` above): discovered empirically while writing this fixture,
        ``PostgresCryptoMarketPulseRepository.save_refresh_run`` genuinely
        conflicts (``CRYPTO_PULSE_PLAN_AS_OF_CONFLICT``) on a second
        same-(plan, UTC day) call, and ``crypto_market_pulse_checkpoint``
        carries a real ``UNIQUE (refresh_id, product_id)`` constraint -- so
        CryptoMarketPulse evidence for one product is captured AT MOST ONCE
        per plan per UTC day, structurally, at the schema level. Since a
        fill requires evidence ``observed_at`` strictly AFTER
        ``order.submitted_at``, and the order's own creation this same day
        already consumed that day's one-and-only checkpoint, a genuine
        same-day fill is impossible for this task's evidence source
        regardless of this milestone's fix -- a separate, pre-existing,
        deeper limitation (this is exactly why the newer strategy-driven
        path moved to the continuously-refreshing LiveMarketObservation
        substrate instead; rewiring THIS older path onto it would be the
        broader PAPER redesign explicitly out of this milestone's scope).
        What the fix genuinely achieves for CRYPTO -- proven below -- is
        that the order survives redispatch at all (never frozen), is
        recognized without duplication, and reconciliation genuinely
        refreshes every tick; the sibling expiry test below covers the
        other half of CRYPTO's real achievable lifecycle."""

        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_crypto_paper_readiness(postgres_engine)
        _build_crypto_allocation(postgres_engine, paper_eligible=True)

        market_pulse = PostgresCryptoMarketPulseRepository(postgres_engine)
        universe, pulse_plan = _seed_crypto_universe_and_plan(
            market_pulse,
            product_id=CRYPTO_PRODUCT_ID,
            plan_id=plan.crypto_pulse_plan_id,
            created_at=NOW,
        )
        _open_crypto_pulse(
            market_pulse,
            plan=pulse_plan,
            universe=universe,
            product_id=CRYPTO_PRODUCT_ID,
            canonical_asset_id="BTC",
            price=Decimal("50000"),
            quote_captured_at=NOW - timedelta(minutes=1),
            as_of=NOW,
        )

        crypto_paper = PostgresCryptoPaperRepository(postgres_engine)
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id = "paper-trial-fixture-plan-worker-scheduler-crypto"

        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        opened = ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "CRYPTO_PAPER_TRIAL_EXECUTION" not in {item.task_type.value for item in opened.tasks}
        assert len(opened_wrapper.results) == 1
        assert opened_wrapper.results[0].awaits_intraday_continuation is True
        assert "CRYPTO_PAPER_ORDERS_CREATED:1" in opened_wrapper.results[0].warnings
        account = crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
        assert account is not None
        assert len(account.orders) == 1
        order_id = account.orders[0].order_id
        first_reconciliation_id = account.reconciliations[-1].reconciliation_id

        # Same cycle_id, later executed_at -- a genuinely new
        # ExecuteOperatingCycle/executor pair (simulated worker restart)
        # over the same repository/state source. This is the core
        # regression proof: pre-fix, ``_resume_cycle`` would find
        # CRYPTO_PAPER_TRIAL_EXECUTION already persisted from the
        # ``opened`` tick above and skip it without ever reaching the
        # executor, so ``later_wrapper.results`` would stay empty here.
        later = NOW + timedelta(minutes=20)
        later_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, later_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=later,
            idempotency_key=f"{cycle_id}-key",
        )
        assert len(later_wrapper.results) == 1
        assert later_wrapper.results[0].awaits_intraday_continuation is True
        # Honestly no fill this tick (see docstring) -- but reconciliation
        # itself must still genuinely refresh, not silently replay tick 1's
        # now-stale record (the exact defect ``deterministic_id`` fixes).
        assert "CRYPTO_PAPER_FILLS_APPLIED:0" in later_wrapper.results[0].warnings

        state = crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate order from the later tick
        still_open_order = next(item for item in state.orders if item.order_id == order_id)
        assert still_open_order.state is PaperOrderState.OPEN
        assert state.reconciliations[-1].reconciliation_id != first_reconciliation_id

    def test_same_cycle_id_later_tick_expires_without_duplicating(
        self, postgres_engine: sa.Engine
    ) -> None:
        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_crypto_paper_readiness(postgres_engine)
        _build_crypto_allocation(postgres_engine, paper_eligible=True)

        market_pulse = PostgresCryptoMarketPulseRepository(postgres_engine)
        universe, pulse_plan = _seed_crypto_universe_and_plan(
            market_pulse,
            product_id=CRYPTO_PRODUCT_ID,
            plan_id=plan.crypto_pulse_plan_id,
            created_at=NOW,
        )
        _open_crypto_pulse(
            market_pulse,
            plan=pulse_plan,
            universe=universe,
            product_id=CRYPTO_PRODUCT_ID,
            canonical_asset_id="BTC",
            price=Decimal("50000"),
            quote_captured_at=NOW - timedelta(minutes=1),
            as_of=NOW,
        )

        crypto_paper = PostgresCryptoPaperRepository(postgres_engine)
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id = "paper-trial-fixture-plan-worker-scheduler-crypto-expiry"

        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id}-key",
        )
        account = crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
        assert account is not None
        order_id = account.orders[0].order_id

        # Same cycle_id, past the real 4-hour CRYPTO_ORDER_VALID_FOR window,
        # with no fresher evidence seeded (STALE) -- must still genuinely
        # redispatch, recognize the already-open order without duplicating
        # it, and expire it on THIS tick -- not 24 hours later at the next
        # UTC day's own first tick.
        much_later = NOW + timedelta(hours=5)
        expiry_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, expiry_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=much_later,
            idempotency_key=f"{cycle_id}-key",
        )
        assert len(expiry_wrapper.results) == 1
        assert expiry_wrapper.results[0].awaits_intraday_continuation is True
        assert "CRYPTO_PAPER_ORDERS_EXPIRED:1" in expiry_wrapper.results[0].warnings

        state = crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate order from the later tick
        expired_order = next(item for item in state.orders if item.order_id == order_id)
        assert expired_order.state is PaperOrderState.EXPIRED


# --------------------------------------------------------------------------
# PAPER Continuation Health Signal v2 (cross-venue extension): the identical
# real-Postgres proof tests/persistence/test_postgres_strategy_paper_trial_
# recurring.py::TestContinuationHealthSignal already gives for
# STRATEGY_PAPER_TRIAL_EXECUTION, now for CRYPTO_PAPER_TRIAL_EXECUTION and
# KALSHI_PAPER_TRIAL_EXECUTION -- both report awaits_intraday_continuation=
# True on every successful dispatch and both reconcile unconditionally on
# every dispatch (confirmed by reading adapters.operating_cycle_tasks.
# _execute_crypto_paper_trial / ._execute_kalshi_paper_trial directly), so
# the identical terminal-status/continuation-evidence divergence
# domain.operating_cycle.compute_paper_continuation_health exists to
# resolve applies to them too.
# --------------------------------------------------------------------------


class _FailOnceThenDelegateWrapper:
    """Raises a genuine exception on the given task type's first dispatch
    (mirroring exactly how a real transient failure reaches
    ExecuteOperatingCycle's existing (OSError, RuntimeError, ValueError)
    _dispatch_task handler and gets persisted as a terminal FAILED
    OperatingTaskResult), then delegates to the real executor on every
    subsequent call. Kept as its own small copy rather than imported from
    test_postgres_strategy_paper_trial_recurring.py's identically-shaped
    class, matching this file's own established convention for
    _RealPaperExecutorWrapper above -- parametrized by task_type so one
    class serves both KALSHI_PAPER_TRIAL_EXECUTION and
    CRYPTO_PAPER_TRIAL_EXECUTION rather than two near-duplicates. Every
    other task type completes trivially."""

    def __init__(
        self, real: ExistingCapabilityOperatingTaskExecutor, *, task_type: OperatingTaskType
    ) -> None:
        self._real = real
        self._task_type = task_type
        self._raised = False

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        if task.task_type is self._task_type:
            if not self._raised:
                self._raised = True
                raise RuntimeError(f"SIMULATED_{self._task_type.value}_TRANSIENT_FAILURE")
            return self._real.execute(
                task=task, cycle_id=cycle_id, plan=plan, as_of=as_of, executed_at=executed_at
            )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED, (f"{task.task_type.value}-output",), (), True, 0
        )


class TestKalshiContinuationHealthSignal:
    """A genuine historical failure on one tick, followed by real healthy
    continuation (a fresh, unconditional reconciliation) on a later real
    day, must classify RECOVERED without ever mutating, hiding, or
    duplicating the original FAILED terminal row. See
    tests/test_operating_cycle.py for full state-machine coverage of
    compute_paper_continuation_health in isolation -- this test proves the
    real Postgres integration only."""

    def test_historical_failure_then_real_recovery_computes_recovered_without_mutating_history(
        self, postgres_engine: sa.Engine
    ) -> None:
        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_paper_readiness(postgres_engine, plan_created_at=plan.created_at)
        _build_allocation(postgres_engine, paper_eligible=True)
        _seed_minimal_crypto_pulse_prerequisite(
            postgres_engine, crypto_pulse_plan_id=plan.crypto_pulse_plan_id, as_of=NOW
        )
        kalshi = PostgresKalshiOperatingRepository(postgres_engine)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="continuation-health-fixture-refresh-day1",
            as_of=NOW,
            price=Decimal("0.45"),
            visible_liquidity=100,
            evidence_observed_at=NOW - timedelta(minutes=1),
        )
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id_day1 = "continuation-health-fixture-plan-worker-kalshi-day1"

        # Tick 1: genuinely succeeds through the real executor -- opens an
        # order; no terminal result persisted (awaits_intraday_continuation).
        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id_day1,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id_day1}-key",
        )
        assert len(opened_wrapper.results) == 1

        # Tick 2, same UTC day/cycle_id: no persisted terminal result yet
        # for this cycle_id (tick 1 deferred it), so the real scheduler
        # genuinely redispatches it -- and this time it genuinely raises,
        # which the real, unmodified exception boundary turns into a
        # terminal FAILED OperatingTaskResult.
        failing_wrapper = _FailOnceThenDelegateWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        failure_tick = NOW + timedelta(minutes=30)
        ExecuteOperatingCycle(plan_repository, state_source, failing_wrapper).execute(
            cycle_id=cycle_id_day1,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=failure_tick,
            idempotency_key=f"{cycle_id_day1}-key",
        )
        failed_results = [
            item
            for item in plan_repository.task_results(cycle_id_day1)
            if item.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        ]
        assert len(failed_results) == 1
        assert failed_results[0].status is OperatingTaskStatus.FAILED
        original_failed_hash = failed_results[0].canonical_hash
        original_failed_at = failed_results[0].completed_at

        # Day 2: a genuinely new cycle_id, real executor throughout, fresh
        # KALSHI refresh evidence (mirroring
        # TestKalshiRealSchedulerIntradayContinuation's own established
        # technique above) -- reconciles unconditionally, producing fresh
        # continuation evidence. Still persists no new terminal result.
        day2 = NOW + timedelta(days=1)
        _save_kalshi_refresh(
            kalshi,
            refresh_id="continuation-health-fixture-refresh-day2",
            as_of=day2,
            price=Decimal("0.45"),
            visible_liquidity=100,
            evidence_observed_at=day2 - timedelta(minutes=1),
        )
        cycle_id_day2 = "continuation-health-fixture-plan-worker-kalshi-day2"
        recovery_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, recovery_wrapper).execute(
            cycle_id=cycle_id_day2,
            plan_id=plan.plan_id,
            as_of=day2,
            executed_at=day2,
            idempotency_key=f"{cycle_id_day2}-key",
        )
        assert len(recovery_wrapper.results) == 1
        assert recovery_wrapper.results[0].awaits_intraday_continuation is True
        assert [
            item
            for item in plan_repository.task_results(cycle_id_day2)
            if item.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        ] == []

        state = kalshi.state(GENUINE_KALSHI_PAPER_ACCOUNT_ID)
        assert state is not None
        latest_reconciliation = max(state.reconciliations, key=lambda item: item.reconciled_at)
        assert latest_reconciliation.reconciled_at > original_failed_at  # genuinely fresher

        now = day2 + timedelta(minutes=1)
        health = compute_paper_continuation_health(
            latest_terminal_status=OperatingTaskStatus.FAILED,
            latest_terminal_at=original_failed_at,
            latest_continuation_at=latest_reconciliation.reconciled_at,
            now=now,
            freshness_window=timedelta(hours=1),
        )
        assert health.state is PaperContinuationHealthState.RECOVERED
        assert health.recovered_since_failure is True
        # The historical failure is preserved and still reported, never
        # hidden -- it is what "recovered" is recovering FROM.
        assert health.latest_terminal_status is OperatingTaskStatus.FAILED
        assert health.latest_terminal_at == original_failed_at

        # The original FAILED row's own persisted content is completely
        # unchanged -- immutable at the DB level (apex_reject_mutation
        # trigger) and never written to by this milestone's purely
        # additive, read-only new code path.
        refetched_failed = [
            item
            for item in plan_repository.task_results(cycle_id_day1)
            if item.task_type is OperatingTaskType.KALSHI_PAPER_TRIAL_EXECUTION
        ]
        assert len(refetched_failed) == 1
        assert refetched_failed[0].canonical_hash == original_failed_hash
        assert refetched_failed[0].status is OperatingTaskStatus.FAILED
        assert refetched_failed[0].completed_at == original_failed_at


class TestCryptoContinuationHealthSignal:
    """The CRYPTO counterpart of TestKalshiContinuationHealthSignal above --
    same real-executor failure-then-recovery proof, using this file's own
    established CRYPTO-venue fixture chain (TestCryptoRealSchedulerIntraday
    Continuation)."""

    def test_historical_failure_then_real_recovery_computes_recovered_without_mutating_history(
        self, postgres_engine: sa.Engine
    ) -> None:
        plan = _plan(postgres_engine)
        _activate_paper(postgres_engine, plan_id=plan.plan_id, created_at=plan.created_at)
        _retain_crypto_paper_readiness(postgres_engine)
        _build_crypto_allocation(postgres_engine, paper_eligible=True)

        market_pulse = PostgresCryptoMarketPulseRepository(postgres_engine)
        universe, pulse_plan = _seed_crypto_universe_and_plan(
            market_pulse,
            product_id=CRYPTO_PRODUCT_ID,
            plan_id=plan.crypto_pulse_plan_id,
            created_at=NOW,
        )
        _open_crypto_pulse(
            market_pulse,
            plan=pulse_plan,
            universe=universe,
            product_id=CRYPTO_PRODUCT_ID,
            canonical_asset_id="BTC",
            price=Decimal("50000"),
            quote_captured_at=NOW - timedelta(minutes=1),
            as_of=NOW,
        )

        crypto_paper = PostgresCryptoPaperRepository(postgres_engine)
        plan_repository = PostgresOperatingCycleRepository(postgres_engine)
        state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
        cycle_id_day1 = "continuation-health-fixture-plan-worker-crypto-day1"

        # Tick 1: genuinely succeeds through the real executor -- opens an
        # order; no terminal result persisted (awaits_intraday_continuation).
        opened_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, opened_wrapper).execute(
            cycle_id=cycle_id_day1,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=NOW,
            idempotency_key=f"{cycle_id_day1}-key",
        )
        assert len(opened_wrapper.results) == 1

        # Tick 2, same UTC day/cycle_id: genuinely raises, which the real,
        # unmodified exception boundary turns into a terminal FAILED
        # OperatingTaskResult.
        failing_wrapper = _FailOnceThenDelegateWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        failure_tick = NOW + timedelta(minutes=30)
        ExecuteOperatingCycle(plan_repository, state_source, failing_wrapper).execute(
            cycle_id=cycle_id_day1,
            plan_id=plan.plan_id,
            as_of=NOW,
            executed_at=failure_tick,
            idempotency_key=f"{cycle_id_day1}-key",
        )
        failed_results = [
            item
            for item in plan_repository.task_results(cycle_id_day1)
            if item.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        ]
        assert len(failed_results) == 1
        assert failed_results[0].status is OperatingTaskStatus.FAILED
        original_failed_hash = failed_results[0].canonical_hash
        original_failed_at = failed_results[0].completed_at

        # Day 2: a genuinely new cycle_id, real executor throughout, a
        # fresh CryptoMarketPulse checkpoint for the new UTC day (required --
        # see _open_crypto_pulse's own docstring on the real per-(plan, UTC
        # day) uniqueness constraint) -- reconciles unconditionally,
        # producing fresh continuation evidence.
        day2 = NOW + timedelta(days=1)
        _open_crypto_pulse(
            market_pulse,
            plan=pulse_plan,
            universe=universe,
            product_id=CRYPTO_PRODUCT_ID,
            canonical_asset_id="BTC",
            price=Decimal("50000"),
            quote_captured_at=day2 - timedelta(minutes=1),
            as_of=day2,
            refresh_id=f"{pulse_plan.plan_id}-refresh-day2",
        )
        cycle_id_day2 = "continuation-health-fixture-plan-worker-crypto-day2"
        recovery_wrapper = _RealPaperExecutorWrapper(
            _executor(postgres_engine), task_type=OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        )
        ExecuteOperatingCycle(plan_repository, state_source, recovery_wrapper).execute(
            cycle_id=cycle_id_day2,
            plan_id=plan.plan_id,
            as_of=day2,
            executed_at=day2,
            idempotency_key=f"{cycle_id_day2}-key",
        )
        assert len(recovery_wrapper.results) == 1
        assert recovery_wrapper.results[0].awaits_intraday_continuation is True
        assert [
            item
            for item in plan_repository.task_results(cycle_id_day2)
            if item.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        ] == []

        state = crypto_paper.state(GENUINE_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        latest_reconciliation = max(state.reconciliations, key=lambda item: item.evaluated_at)
        assert latest_reconciliation.evaluated_at > original_failed_at  # genuinely fresher

        now = day2 + timedelta(minutes=1)
        health = compute_paper_continuation_health(
            latest_terminal_status=OperatingTaskStatus.FAILED,
            latest_terminal_at=original_failed_at,
            latest_continuation_at=latest_reconciliation.evaluated_at,
            now=now,
            freshness_window=timedelta(hours=1),
        )
        assert health.state is PaperContinuationHealthState.RECOVERED
        assert health.recovered_since_failure is True
        assert health.latest_terminal_status is OperatingTaskStatus.FAILED
        assert health.latest_terminal_at == original_failed_at

        refetched_failed = [
            item
            for item in plan_repository.task_results(cycle_id_day1)
            if item.task_type is OperatingTaskType.CRYPTO_PAPER_TRIAL_EXECUTION
        ]
        assert len(refetched_failed) == 1
        assert refetched_failed[0].canonical_hash == original_failed_hash
        assert refetched_failed[0].status is OperatingTaskStatus.FAILED
        assert refetched_failed[0].completed_at == original_failed_at
