from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    PostgresLedgerRepository,
    postgres_autopilot_store,
    postgres_evidence_review_store,
    postgres_operator_console_store,
    postgres_paper_portfolio_store,
    postgres_strategy_shadow_store,
    securities_fill_evidence,
    securities_paper_order,
    securities_position_state,
)
from apex.adapters.securities_data import (
    LocalSecuritiesDatasetAdapter,
    securities_record,
)
from apex.adapters.securities_paper_postgres import (
    PostgresSecuritiesPaperCampaignBoundary,
    PostgresSecuritiesPaperRepository,
)
from apex.adapters.securities_paper_synthetic import _review_source
from apex.adapters.securities_postgres import (
    PostgresSecuritiesCampaignBoundary,
    PostgresSecuritiesRepository,
)
from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyEvidenceState,
    StrategyFamily,
    StrategyImplementationState,
)
from apex.application.evidence_review import (
    BuildStrategyEvidenceSnapshot,
    BuildStrategyEvidenceSnapshotCommand,
    DecideStrategyEvidenceReview,
    DecideStrategyEvidenceReviewCommand,
    InMemoryEvidenceReviewStore,
    PaperTrialAuthorization,
    PaperTrialLimits,
    RequestStrategyEvidenceReview,
    RequestStrategyEvidenceReviewCommand,
    StartStrategyEvidenceReview,
    StrategyEvidenceReviewPolicy,
    StrategyEvidenceReviewState,
    paper_trial_confirmation_text,
)
from apex.application.operator_console import (
    CreateMoonshotCampaign,
    CreateMoonshotCampaignCommand,
    OperatorMode,
)
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    RunSecuritiesResearch,
    RunSecuritiesResearchCommand,
)
from apex.application.securities_paper import (
    ApplySecuritiesCorporateActions,
    BuildSecuritiesPaperOpportunities,
    BuildSecuritiesPaperOpportunityCommand,
    CreateSecuritiesPaperRun,
    CreateSecuritiesPaperRunCommand,
    ExecuteSecuritiesPaperOpportunityCommand,
    ExitSecuritiesPositionCommand,
    ExitSecuritiesPositions,
    MarkSecuritiesPositions,
    RunSecuritiesPaperExecution,
    complete_run,
)
from apex.application.strategy_shadow import StrategyAuthorityCommand
from apex.domain.autopilot import EvidenceTier
from apex.domain.paper import (
    PaperExecutionModel,
    PaperExecutionPolicy,
    PaperUnfilledBehavior,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesInstrument,
    SecuritiesSignalDirection,
    SecuritiesStrategySpecification,
)
from apex.domain.securities_paper import (
    SecuritiesExecutionPolicy,
    SecuritiesExitReason,
    SecuritiesPaperRunState,
    SecuritiesPositionStatus,
)
from apex.domain.values import ContentHash, Money, Quantity, RiskFraction, content_digest

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
AUTHORIZATION_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
CAMPAIGN_ID = "campaign-securities-paper-persistence"


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _stage_one_source(
    adapter: LocalSecuritiesDatasetAdapter,
) -> tuple[str, tuple[str, ...], str, str]:
    dates = tuple(date(2026, 7, 20) + timedelta(days=index) for index in range(6))
    sessions = tuple(
        SecuritiesCalendarSession.create(
            calendar_identity="XNYS",
            calendar_version="securities-paper-persistence-v1",
            timezone="America/New_York",
            session_date=value.isoformat(),
            opens_at=datetime.combine(value, time(14, 30), tzinfo=UTC),
            closes_at=datetime.combine(value, time(21), tzinfo=UTC),
            is_early_close=False,
            source_identity="securities-paper-persistence-calendar",
        )
        for value in dates
    )
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=f"securities-paper-persistence-{index}",
            symbol=f"SP{index}",
            asset_class="ETF" if index == 0 else "COMMON_STOCK",
            exchange_mic="XNYS",
            currency="USD",
            timezone="America/New_York",
            valid_from=sessions[0].session_date,
            valid_to=None,
            source_identity="securities-paper-persistence-instruments",
        )
        for index in range(2)
    )
    bars = tuple(
        SecuritiesBarEvidence(
            instrument_id=instrument.instrument_id,
            interval="ONE_DAY",
            adjustment_state="RAW",
            period_start=session.opens_at,
            period_end=session.closes_at,
            available_at=session.closes_at + timedelta(minutes=1),
            open=str(50 + instrument_index + session_index),
            high=str(52 + instrument_index + session_index),
            low=str(49 + instrument_index + session_index),
            close=str(51 + instrument_index + session_index),
            volume=str(10_000 + session_index),
            source_identity="securities-paper-persistence-bars",
        )
        for instrument_index, instrument in enumerate(instruments)
        for session_index, session in enumerate(sessions)
    )
    actions = (
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[1].instrument_id,
            action_type="CASH_DIVIDEND",
            effective_date=sessions[4].session_date,
            available_at=sessions[3].closes_at,
            cash_amount="0.1",
            source_identity="securities-paper-persistence-actions",
        ),
    )
    source = adapter.write_generated_jsonl(
        relative_location="incoming/persistence-paper.jsonl",
        records=(securities_record(item) for item in (*instruments, *sessions, *bars, *actions)),
    )
    return (
        source,
        tuple(item.instrument_id for item in instruments),
        sessions[0].session_date,
        sessions[-1].session_date,
    )


def _governed_trial(
    *,
    engine: Engine,
    descriptor: StrategyDescriptor,
    policy: SecuritiesExecutionPolicy,
) -> PaperTrialAuthorization:
    review_policy = StrategyEvidenceReviewPolicy(
        policy_id="persistence-securities-paper-review-policy",
        policy_version="v1",
        supported_strategy_family=None,
        supported_strategy_id=descriptor.strategy_id,
        minimum_resolved_predictions=6,
        minimum_distinct_events=6,
        minimum_distinct_markets=6,
        minimum_probability_range_coverage=3,
        maximum_unresolved_integrity_warnings=0,
        maximum_point_brier_difference=Decimal(1),
        maximum_conservative_brier_difference=Decimal(1),
        maximum_point_log_loss_difference=Decimal(1),
        maximum_conservative_log_loss_difference=Decimal(1),
        maximum_recent_degradation=Decimal(1),
        minimum_recent_resolved=6,
        minimum_calibration_coverage=Decimal("0.6"),
        maximum_conflict_frequency=Decimal(0),
        maximum_evidence_age=timedelta(days=30),
        require_configuration_lineage=True,
        require_outcome_lineage=True,
        configuration_hash=_hash("persistence-review-policy"),
        created_at=AUTHORIZATION_NOW - timedelta(days=30),
    )
    fixture_store = InMemoryEvidenceReviewStore(
        policies=(review_policy,),
        sources=(_review_source(descriptor),),
    )
    snapshot, criteria = BuildStrategyEvidenceSnapshot(fixture_store).build(
        BuildStrategyEvidenceSnapshotCommand(
            strategy_id=descriptor.strategy_id,
            strategy_version=descriptor.version,
            configuration_hash=descriptor.configuration_hash,
            policy_id=review_policy.policy_id,
            policy_version=review_policy.policy_version,
            cutoff_at=AUTHORIZATION_NOW,
            full_history_started_at=AUTHORIZATION_NOW - timedelta(days=30),
            recent_history_started_at=AUTHORIZATION_NOW - timedelta(days=20),
            idempotency_key="persistence-securities-snapshot",
        )
    )
    review_store = postgres_evidence_review_store(engine)
    review_store.ensure_policy(review_policy)
    review_store.save_snapshot(
        snapshot,
        criteria,
        idempotency_key="persistence-securities-snapshot",
    )
    requested = RequestStrategyEvidenceReview(review_store).request(
        RequestStrategyEvidenceReviewCommand(
            strategy_id=descriptor.strategy_id,
            strategy_version=descriptor.version,
            snapshot_id=snapshot.snapshot_id,
            campaign_id=CAMPAIGN_ID,
            expires_at=AUTHORIZATION_NOW + timedelta(hours=4),
            operator_reason="review persistence Securities fixture evidence",
            operator_note="NO BROKER OR LIVE MARKET AUTHORITY",
            idempotency_key="persistence-securities-review-request",
        ),
        now=AUTHORIZATION_NOW,
    )
    started = StartStrategyEvidenceReview(review_store).start(
        review_id=requested.review_id,
        operator_reason="start persistence Securities review",
        operator_note="fixture only",
        idempotency_key="persistence-securities-review-start",
        now=AUTHORIZATION_NOW,
    )
    legacy_policy_anchor = PaperExecutionPolicy(
        policy_version_id=policy.policy_id,
        execution_model=PaperExecutionModel.IMMEDIATE_CROSS_V1,
        maximum_evidence_age_seconds=3600,
        maximum_spread=Decimal("1"),
        maximum_orderbook_depth=1,
        maximum_visible_size_participation=policy.volume_participation_cap,
        slippage_reserve_per_contract=Money(policy.slippage_per_share),
        fee_model_version="SECURITIES_PER_SHARE_V1",
        time_in_force_seconds=86400,
        unfilled_behavior=PaperUnfilledBehavior.EXPIRE,
        configuration_version=policy.policy_version,
        configuration_hash=policy.canonical_hash,
    )
    postgres_paper_portfolio_store(engine).ensure_execution_policy(
        legacy_policy_anchor,
        recorded_at=AUTHORIZATION_NOW,
    )
    approved, trial = DecideStrategyEvidenceReview(review_store).decide(
        DecideStrategyEvidenceReviewCommand(
            review_id=started.review_id,
            decision=StrategyEvidenceReviewState.APPROVED,
            operator_reason="approve bounded persistence Securities trial",
            operator_note="fixture only",
            idempotency_key="persistence-securities-review-approve",
            confirmation_text=paper_trial_confirmation_text(
                strategy_id=descriptor.strategy_id,
                campaign_id=CAMPAIGN_ID,
                snapshot_id=snapshot.snapshot_id,
            ),
        ),
        now=AUTHORIZATION_NOW,
        trial_limits=PaperTrialLimits(
            paper_execution_policy_id=policy.policy_id,
            paper_execution_policy_hash=policy.canonical_hash,
            starts_at=AUTHORIZATION_NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(days=10),
            maximum_paper_cycles=4,
            maximum_approved_opportunities=4,
            maximum_paper_orders=6,
            maximum_simultaneous_open_positions=3,
            maximum_per_opportunity_risk_fraction=RiskFraction("0.3"),
            maximum_strategy_active_risk_fraction=RiskFraction("0.8"),
            maximum_cumulative_authorized_risk=Money("9000"),
            maximum_cumulative_realized_paper_loss=Money("1000"),
            maximum_paper_drawdown=Money("1000"),
            maximum_capital_lockup_seconds=86400,
            allowed_instrument_class="CONTINUOUS_SECURITIES",
            allowed_provider="FIXTURE_MANAGED_SECURITIES_DATA",
        ),
    )
    assert approved.state is StrategyEvidenceReviewState.APPROVED
    assert trial is not None
    authority = postgres_strategy_shadow_store(engine).change_authority(
        StrategyAuthorityCommand(
            strategy_id=descriptor.strategy_id,
            requested_authority=StrategyAuthorityState.PAPER,
            operator_reason="activate exact bounded Securities trial",
            evidence_review_id=approved.review_id,
            idempotency_key="persistence-securities-authority",
            confirmation_text="explicit persistence fixture confirmation",
            requested_at=AUTHORIZATION_NOW,
            paper_trial_authorization_id=trial.authorization_id,
        )
    )
    assert authority.resulting_authority is StrategyAuthorityState.PAPER
    return trial


def test_securities_paper_is_atomic_idempotent_and_restart_safe(
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    CreateMoonshotCampaign(postgres_operator_console_store(postgres_engine)).create(
        CreateMoonshotCampaignCommand(
            campaign_id=CAMPAIGN_ID,
            name="Securities PAPER persistence Campaign",
            initial_capital=Money("10000"),
            maximum_authorized_loss=Money("5000"),
            starts_at=AUTHORIZATION_NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=10),
            operating_mode=OperatorMode.PAPER,
            maximum_active_risk_fraction=RiskFraction("0.8"),
            maximum_per_opportunity_risk_fraction=RiskFraction("0.3"),
            maximum_per_strategy_risk_fraction=RiskFraction("0.8"),
            maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.8"),
            maximum_simultaneous_reservations=4,
            maximum_simultaneous_positions=4,
            maximum_capital_lockup_seconds=86400,
            minimum_conservative_expected_value=Money("0"),
            minimum_evidence_tier=EvidenceTier.RESEARCH,
            allow_profit_compounding=False,
            operator_note="NO BROKER OR LIVE MARKET AUTHORITY",
            idempotency_key="persistence-securities-campaign",
        )
    )
    data_adapter = LocalSecuritiesDatasetAdapter(tmp_path / "managed")
    source, universe, start_date, end_date = _stage_one_source(data_adapter)
    stage_one = PostgresSecuritiesRepository(postgres_engine)
    batch = ImportSecuritiesData(
        repository=stage_one,
        market_data=data_adapter,
    ).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="Securities PAPER persistence evidence",
            idempotency_key="persistence-paper-import",
            imported_at=NOW,
        )
    )
    specification = SecuritiesStrategySpecification.create(
        strategy_id="persistence-securities-paper-v1",
        kind="TIME_SERIES_MOMENTUM",
        version="1",
        lookback_sessions=2,
        rebalance_every_sessions=1,
        created_at=NOW,
    )
    stage_one.save_specification(specification)
    research = RunSecuritiesResearch(
        repository=stage_one,
        campaigns=PostgresSecuritiesCampaignBoundary(postgres_engine),
        market_data=data_adapter,
    ).execute(
        RunSecuritiesResearchCommand(
            campaign_id=CAMPAIGN_ID,
            strategy_id=specification.strategy_id,
            data_batch_id=batch.batch_id,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            decision_cutoff=NOW,
            benchmark_instrument_id=universe[0],
            idempotency_key="persistence-paper-research",
        )
    )
    signals = tuple(
        item
        for item in stage_one.list_signals(research.research_run_id)
        if item.direction is SecuritiesSignalDirection.LONG and item.instrument_id == universe[1]
    )
    assert signals
    descriptor = StrategyDescriptor(
        strategy_id=specification.strategy_id,
        version=specification.version,
        display_name="Persistence Securities PAPER",
        family=StrategyFamily.SECURITIES,
        implementation_state=StrategyImplementationState.FIXTURE,
        authority_state=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.FIXTURE,
        last_evidence_decision="persistence-securities-review",
        supported_instrument_class="CONTINUOUS_SECURITIES",
        maximum_proposals_per_cycle=4,
        configuration_hash=ContentHash(specification.canonical_hash),
    )
    postgres_autopilot_store(postgres_engine).register_descriptors((descriptor,))
    policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=AUTHORIZATION_NOW)
    trial = _governed_trial(
        engine=postgres_engine,
        descriptor=descriptor,
        policy=policy,
    )
    governed = postgres_evidence_review_store(postgres_engine).descriptor(descriptor.strategy_id)
    repository = PostgresSecuritiesPaperRepository(postgres_engine)
    campaigns = PostgresSecuritiesPaperCampaignBoundary(postgres_engine)
    run = CreateSecuritiesPaperRun(
        repository=repository,
        campaigns=campaigns,
    ).execute(
        CreateSecuritiesPaperRunCommand(
            campaign_id=CAMPAIGN_ID,
            trial=trial,
            descriptor=governed,
            data_batch_id=batch.batch_id,
            data_batch_hash=ContentHash(batch.canonical_hash),
            policy=policy,
            start_date=start_date,
            end_date=end_date,
            signal_count=len(signals),
            idempotency_key="persistence-paper-run",
            requested_at=AUTHORIZATION_NOW,
        )
    )
    _instruments, _sessions, bars, actions = stage_one.get_dataset(batch.batch_id)
    signal = signals[0]
    instrument_bars = tuple(
        sorted(
            (item for item in bars if item.instrument_id == signal.instrument_id),
            key=lambda item: item.period_start,
        )
    )
    eligible = next(item for item in instrument_bars if item.period_start > signal.signal_at)
    opportunity = BuildSecuritiesPaperOpportunities(repository).execute(
        BuildSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,
            signal=signal,
            next_bar=eligible,
            requested_quantity=Quantity("10", "1"),
            policy=policy,
            holding_horizon_sessions=3,
        )
    )
    executor = RunSecuritiesPaperExecution(
        repository=repository,
        campaigns=campaigns,
    )
    command = ExecuteSecuritiesPaperOpportunityCommand(
        run_id=run.run_id,
        opportunity=opportunity,
        signal=signal,
        next_bar=eligible,
        policy=policy,
        trial=trial,
        strategy_authority_decision_id=governed.last_evidence_decision,
        symbol="SP1",
        idempotency_key="persistence-paper-order",
        expires_at=eligible.period_end,
    )
    opened = executor.execute(command)
    replayed = executor.execute(command)
    assert opened.order.order_id == replayed.order.order_id
    assert opened.position is not None
    assert repository.run(run.run_id).order_count == 1
    assert repository.status(CAMPAIGN_ID)["position_cost"] == opened.fill.notional.amount
    open_usage = postgres_evidence_review_store(postgres_engine).trial(trial.authorization_id).usage
    assert open_usage.paper_orders == 1
    assert open_usage.open_positions == 1
    assert open_usage.active_strategy_risk.amount > 0

    second_signal = signals[1]
    second_bar = next(
        item for item in instrument_bars if item.period_start > second_signal.signal_at
    )
    second_opportunity = BuildSecuritiesPaperOpportunities(repository).execute(
        BuildSecuritiesPaperOpportunityCommand(
            run_id=run.run_id,
            signal=second_signal,
            next_bar=second_bar,
            requested_quantity=Quantity("2", "1"),
            policy=policy,
            holding_horizon_sessions=2,
        )
    )
    before = repository.presentation(CAMPAIGN_ID)
    with (
        patch.object(
            PostgresLedgerRepository,
            "post_securities_journal",
            side_effect=RuntimeError("forced ledger rollback"),
        ),
        pytest.raises(RuntimeError, match="forced ledger rollback"),
    ):
        executor.execute(
            ExecuteSecuritiesPaperOpportunityCommand(
                run_id=run.run_id,
                opportunity=second_opportunity,
                signal=second_signal,
                next_bar=second_bar,
                policy=policy,
                trial=trial,
                strategy_authority_decision_id=governed.last_evidence_decision,
                symbol="SP1",
                idempotency_key="persistence-paper-rollback",
                expires_at=second_bar.period_end,
            )
        )
    after = repository.presentation(CAMPAIGN_ID)
    assert len(after["orders"]) == len(before["orders"])  # type: ignore[arg-type]
    assert len(after["fills"]) == len(before["fills"])  # type: ignore[arg-type]
    assert len(after["positions"]) == len(before["positions"])  # type: ignore[arg-type]
    assert len(after["journals"]) == len(before["journals"])  # type: ignore[arg-type]

    later_bars = tuple(
        item for item in instrument_bars if item.available_at > eligible.available_at
    )
    assert len(later_bars) >= 2
    marked = MarkSecuritiesPositions(repository).execute(
        position_id=opened.position.position_id,
        bar=later_bars[0],
        policy=policy,
        as_of=later_bars[0].available_at,
    )
    replayed_mark = MarkSecuritiesPositions(repository).execute(
        position_id=opened.position.position_id,
        bar=later_bars[0],
        policy=policy,
        as_of=later_bars[0].available_at,
    )
    assert replayed_mark.mark.mark_id == marked.mark.mark_id
    dividend = next(item for item in actions if item.instrument_id == opened.position.instrument_id)
    applied = ApplySecuritiesCorporateActions(repository).execute(
        position_id=opened.position.position_id,
        action=dividend,
        applied_at=later_bars[0].available_at,
        quantity_increment=Decimal("1"),
    )
    assert applied.application.cash_entitlement == Money("1")
    exit_result = ExitSecuritiesPositions(repository).execute(
        ExitSecuritiesPositionCommand(
            position_id=opened.position.position_id,
            reason=SecuritiesExitReason.OPERATOR_EXIT,
            decision_at=later_bars[0].available_at,
            next_bar=later_bars[1],
            requested_quantity=opened.position.open_quantity,
            policy=policy,
            source_identity="persistence-operator-exit",
            idempotency_key="persistence-paper-exit",
            expires_at=later_bars[1].period_end,
        )
    )
    assert exit_result.position.state is SecuritiesPositionStatus.CLOSED
    completed = complete_run(
        repository.run(run.run_id),
        repository=repository,
        completed_at=later_bars[1].available_at,
    )
    assert completed.state is SecuritiesPaperRunState.COMPLETED
    restarted = PostgresSecuritiesPaperRepository(postgres_engine)
    assert restarted.run(run.run_id) == completed
    assert restarted.status(CAMPAIGN_ID)["position_cost"] == Decimal(0)
    completed_usage = (
        postgres_evidence_review_store(postgres_engine).trial(trial.authorization_id).usage
    )
    assert completed_usage.completed_cycles == 1
    assert completed_usage.approved_opportunities == 2
    assert completed_usage.paper_orders == 2
    assert completed_usage.open_positions == 0
    assert completed_usage.cumulative_authorized_risk.amount > 0
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_paper_order)) == 2
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_fill_evidence)) == 2
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(securities_position_state))
            == 1
        )
