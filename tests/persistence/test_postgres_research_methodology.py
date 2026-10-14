"""PostgreSQL persistence proofs for quant research methodology and walk-
forward validation: migration 0079 round-trip through fresh repository
instances for all nine tables, the walk-forward-run guard trigger (mutable
cursor/completion fields only, terminal states final), immutable-evidence
enforcement for the other eight tables, and downgrade protection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
from apex.adapters.research_methodology_postgres import (
    RESEARCH_METHODOLOGY_TABLES,
    PostgresResearchMethodologyRepository,
)
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.historical_replay import (
    CreateReplayRun,
    CreateReplayRunCommand,
    HistoricalReplayEngine,
    StartReplayRun,
    StepReplayRunCommand,
)
from apex.application.research_methodology import (
    BuildConclusionCommand,
    BuildResearchConclusion,
    BuildResearchTrialFamily,
    BuildTrialFamilyCommand,
    CreateWalkForwardRun,
    CreateWalkForwardRunCommand,
    DeclareGovernancePolicyCommand,
    DeclareResearchGovernancePolicy,
    EvaluatePromotion,
    EvaluatePromotionCommand,
    RecordHoldoutExposure,
    RecordHoldoutExposureCommand,
    RecordResearchTrial,
    RecordResearchTrialCommand,
    RunRobustnessChecks,
    RunRobustnessChecksCommand,
    StartWalkForwardRun,
    StepWalkForwardRunCommand,
    WalkForwardEngine,
)
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.historical_replay import (
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
)
from apex.domain.research_methodology import (
    BenchmarkKind,
    ConfigurationOrigin,
    GovernanceIntent,
    MultipleTestingCorrectionMethod,
    PromotionPolicy,
    ResearchConfiguration,
    ResearchPhase,
    TrainingWindowMode,
    TrialSelectionMethod,
    WalkForwardCompletionState,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesDataBatchState,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _dates(start: str, count: int) -> list[str]:
    from datetime import date as _date

    start_date = _date.fromisoformat(start)
    return [(start_date + timedelta(days=index)).isoformat() for index in range(count)]


def _session(day: str) -> SecuritiesCalendarSession:
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="test-v1",
        timezone="America/New_York",
        session_date=day,
        opens_at=datetime.fromisoformat(f"{day}T14:30:00+00:00"),
        closes_at=datetime.fromisoformat(f"{day}T21:00:00+00:00"),
        is_early_close=False,
        source_identity="TEST_GENUINE_TABLE_CALENDAR",
    )


def _bar(instrument_id: str, day: str, close: str) -> SecuritiesBarEvidence:
    period_start = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T21:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(minutes=30),
        open=close,
        high=str(Decimal(close) + 1),
        low=str(Decimal(close) - 1),
        close=close,
        volume="1000",
        source_identity="TEST_GENUINE_TABLE",
    )


def _build_replay_run(*, postgres_engine: Engine, day_count: int, idempotency_key: str):
    """Real snapshot -> real dataset -> real completed replay run, mirroring
    ``test_postgres_historical_replay.py``'s own fixture exactly, since
    every research-methodology table binds against a genuine
    ``historical_replay_run`` foreign key."""

    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    securities_repository = PostgresSecuritiesRepository(postgres_engine)
    dates = _dates("2021-01-01", day_count)
    policy = HistoricalCapturePolicy.create(
        provider="FIXTURE_PROVIDER",
        start_date=dates[0],
        end_date=dates[-1],
        chunk_size=100,
        maximum_instruments=100,
        maximum_requests=100,
        maximum_bytes=50_000_000,
    )
    universe = (
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable-AAA", symbol_at_capture="AAA", asset_class="COMMON_STOCK"
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=universe,
        evidence_class="FIXTURE",
        operator_action_id="op-fixture",
        idempotency_key=idempotency_key,
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=member.instrument_id,
            symbol=member.symbol_at_capture,
            asset_class=SecuritiesAssetClass.COMMON_STOCK,
            exchange_mic="XNYS",
            currency="USD",
            timezone="America/New_York",
            valid_from="0001-01-01",
            valid_to=None,
            source_identity="TEST_GENUINE_TABLE",
        )
        for member in universe
    )
    sessions = tuple(_session(day) for day in dates)
    bars: list[SecuritiesBarEvidence] = []
    for index, day in enumerate(dates):
        # Alternating blocks so a benchmark momentum signal genuinely varies.
        block = 20
        up_phase = (index // block) % 2 == 0
        step = Decimal("0.5") if up_phase else Decimal("-0.4")
        full_blocks = index // block
        price = Decimal("100")
        for completed_block in range(full_blocks):
            price += (Decimal("0.5") if completed_block % 2 == 0 else Decimal("-0.4")) * Decimal(
                block
            )
        price += step * Decimal(index % block)
        bars.append(_bar(instruments[0].instrument_id, day, str(max(Decimal("2"), price))))
    batch = SecuritiesMarketDataBatch(
        batch_id=f"batch-{plan.plan_id}",
        dataset_name="fixture",
        state=SecuritiesDataBatchState.READY,
        imported_at=NOW,
        source_artifact="x",
        source_sha256="a" * 64,
        source_byte_size=1,
        canonical_artifact="y",
        canonical_sha256="b" * 64,
        canonical_byte_size=1,
        instrument_count=len(instruments),
        session_count=len(sessions),
        bar_count=len(bars),
        corporate_action_count=0,
        first_period_start=datetime.fromisoformat(f"{dates[0]}T14:30:00+00:00"),
        last_period_end=datetime.fromisoformat(f"{dates[-1]}T21:00:00+00:00"),
        idempotency_key=f"batch-{idempotency_key}",
        warnings=(),
        canonical_hash=content_digest(f"batch-{idempotency_key}"),
    )
    securities_repository.save_dataset(
        batch=batch, instruments=instruments, sessions=sessions, bars=bars, corporate_actions=()
    )
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(batch.batch_id,),
        source_operation_ids=("op-1",),
        observation_count=len(bars),
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date=dates[0],
        latest_observation_date=dates[-1],
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)

    replay_repository = PostgresHistoricalReplayRepository(postgres_engine)
    run = CreateReplayRun(
        repository=replay_repository, historical_data_repository=historical_repository
    ).execute(
        CreateReplayRunCommand(
            dataset_snapshot_id=snapshot.snapshot_id,
            start_date=snapshot.start_date,
            end_date=snapshot.end_date,
            calendar_identity=None,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key=f"{idempotency_key}-run",
            created_at=NOW,
        )
    )
    engine = HistoricalReplayEngine(
        repository=replay_repository,
        historical_data_repository=historical_repository,
        evidence=PostgresSecuritiesRepository(postgres_engine),
    )
    completed = StartReplayRun(repository=replay_repository, engine=engine).execute(
        StepReplayRunCommand(
            replay_run_id=run.replay_run_id,
            maximum_steps=day_count,
            requested_at=NOW,
            benchmark_stable_security_id="stable-AAA",
        )
    )
    return replay_repository, historical_repository, completed


def _configuration(
    *, origin: ConfigurationOrigin = ConfigurationOrigin.FIXED_A_PRIORI
) -> ResearchConfiguration:
    return ResearchConfiguration.create(
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        method_version="1",
        parameter_values=(("lookback_sessions", "20"),),
        origin=origin,
        candidates_examined_count=1,
    )


def test_research_methodology_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_METHODOLOGY_TABLES)


def test_governance_policy_and_holdout_exposure_round_trip(postgres_engine: Engine) -> None:
    _replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=120, idempotency_key="gov-plan"
    )
    period_policy = ResearchPeriodPolicy.create(
        replay_run_id=run.replay_run_id,
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2021-01-01", "2021-03-01", False),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2021-03-02", "2021-04-30", True),
        ),
        minimum_sample_count=10,
        created_at=NOW,
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    governance = DeclareResearchGovernancePolicy(repository=repository).execute(
        DeclareGovernancePolicyCommand(
            hypothesis_version_id="hv-1",
            dataset_snapshot_id=run.dataset_snapshot_id,
            replay_run_id=run.replay_run_id,
            intent=GovernanceIntent.PROMOTION_TRACK,
            period_policy=period_policy,
            revision=1,
            supersedes_governance_policy_id=None,
            declared_at=NOW,
        )
    )
    fresh = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded = fresh.governance_policy_by_id(governance.governance_policy_id)
    assert reloaded == governance
    assert reloaded.period_policy == period_policy

    exposure = RecordHoldoutExposure(repository=fresh).execute(
        RecordHoldoutExposureCommand(
            governance_policy_id=governance.governance_policy_id,
            exposed_at=NOW,
            actor="researcher-1",
            purpose="EVALUATION_ONLY",
            consumed_trial_id=None,
        )
    )
    fresh_again = PostgresResearchMethodologyRepository(postgres_engine)
    exposures = fresh_again.holdout_exposures_for_policy(governance.governance_policy_id)
    assert exposures == (exposure,)


def test_trial_and_trial_family_round_trip(postgres_engine: Engine) -> None:
    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=120, idempotency_key="trial-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    trial = RecordResearchTrial(repository=repository, historical_replay=replay_repository).execute(
        RecordResearchTrialCommand(
            hypothesis_version_id="hv-1",
            dataset_snapshot_id=run.dataset_snapshot_id,
            replay_run_id=run.replay_run_id,
            phase=ResearchPhase.EXPLORATORY,
            market_scope=("US_EQUITY",),
            configuration=_configuration(),
            method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
            session_date_start=run.start_date,
            session_date_end=run.end_date,
            benchmark_kind=BenchmarkKind.UNCONDITIONAL_SUBSEQUENT_RETURN,
            created_at=NOW,
        )
    )
    fresh = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded = fresh.trial_by_id(trial.trial_id)
    assert reloaded == trial
    assert reloaded.diagnostics.base.sample_count > 0
    assert fresh.trials_for_replay_run(run.replay_run_id) == (trial,)

    family = BuildResearchTrialFamily(repository=fresh).execute(
        BuildTrialFamilyCommand(
            hypothesis_id="hv-1",
            trial_ids=(trial.trial_id,),
            candidate_count=3,
            asset_count=1,
            outcome_definition_count=1,
            regime_slice_count=1,
            selection_method=TrialSelectionMethod.PREDECLARED_SINGLE,
            correction_method=MultipleTestingCorrectionMethod.BONFERRONI,
            created_at=NOW,
        )
    )
    fresh_again = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded_family = fresh_again.trial_family_by_id(family.trial_family_id)
    assert reloaded_family == family
    assert fresh_again.trial_families_for_hypothesis("hv-1") == (family,)


def test_walk_forward_run_and_fold_round_trip_and_resume(postgres_engine: Engine) -> None:
    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=200, idempotency_key="wf-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    wf_run = CreateWalkForwardRun(
        repository=repository, historical_replay=replay_repository
    ).execute(
        CreateWalkForwardRunCommand(
            replay_run_id=run.replay_run_id,
            method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
            feature_policy_hash=run.feature_policy_hash,
            outcome_metric_id="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1",
            training_window_sessions=60,
            validation_window_sessions=0,
            test_window_sessions=20,
            step_sessions=20,
            window_mode=TrainingWindowMode.ROLLING,
            minimum_samples=5,
            cost_information_state="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL",
            start_date=run.start_date,
            end_date=run.end_date,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="wf-run-1",
            created_at=NOW,
        )
    )
    fresh = PostgresResearchMethodologyRepository(postgres_engine)
    assert fresh.get_walk_forward_run(wf_run.walk_forward_run_id) == wf_run
    assert wf_run.completion_state is WalkForwardCompletionState.NOT_STARTED

    engine = WalkForwardEngine(repository=fresh, historical_replay=replay_repository)
    started = StartWalkForwardRun(repository=fresh, engine=engine).execute(
        StepWalkForwardRunCommand(
            walk_forward_run_id=wf_run.walk_forward_run_id,
            maximum_folds=1,
            market_scope=("US_EQUITY",),
            configuration=_configuration(),
            benchmark_kind=BenchmarkKind.UNCONDITIONAL_SUBSEQUENT_RETURN,
            requested_at=NOW,
        )
    )
    assert started.completed_fold_count == 1
    fresh_after = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded_run = fresh_after.get_walk_forward_run(wf_run.walk_forward_run_id)
    assert reloaded_run is not None
    assert reloaded_run.completed_fold_count == 1
    folds = fresh_after.folds_for_run(wf_run.walk_forward_run_id)
    assert len(folds) == 1
    assert folds[0].fold_ordinal == 1
    reloaded_fold = folds[0]
    assert reloaded_fold.test_start > reloaded_fold.train_end


def test_robustness_conclusion_and_promotion_round_trip(postgres_engine: Engine) -> None:
    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=120, idempotency_key="rc-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    trial = RecordResearchTrial(repository=repository, historical_replay=replay_repository).execute(
        RecordResearchTrialCommand(
            hypothesis_version_id="hv-rc",
            dataset_snapshot_id=run.dataset_snapshot_id,
            replay_run_id=run.replay_run_id,
            phase=ResearchPhase.VALIDATION,
            market_scope=("US_EQUITY",),
            configuration=_configuration(),
            method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
            session_date_start=run.start_date,
            session_date_end=run.end_date,
            benchmark_kind=BenchmarkKind.UNCONDITIONAL_SUBSEQUENT_RETURN,
            created_at=NOW,
        )
    )
    fresh = PostgresResearchMethodologyRepository(postgres_engine)
    robustness = RunRobustnessChecks(repository=fresh, historical_replay=replay_repository).execute(
        RunRobustnessChecksCommand(
            base_trial_id=trial.trial_id,
            momentum_lookback_windows=(5, 20),
            momentum_positive_threshold=Decimal(0),
            created_at=NOW,
        )
    )
    fresh_after_robustness = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded_robustness = fresh_after_robustness.robustness_run_by_id(robustness.robustness_run_id)
    assert reloaded_robustness == robustness

    conclusion = BuildResearchConclusion(repository=fresh_after_robustness).execute(
        BuildConclusionCommand(
            hypothesis_version_id="hv-rc",
            dataset_snapshot_id=run.dataset_snapshot_id,
            replay_run_id=run.replay_run_id,
            training_trial_id=None,
            validation_trial_id=trial.trial_id,
            holdout_trial_id=None,
            walk_forward_run_id=None,
            robustness_run_id=robustness.robustness_run_id,
            trial_family_id=None,
            point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
            rationale="Genuine Postgres round-trip proof.",
            created_at=NOW,
        )
    )
    fresh_after_conclusion = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded_conclusion = fresh_after_conclusion.conclusion_by_id(conclusion.conclusion_id)
    assert reloaded_conclusion == conclusion
    assert fresh_after_conclusion.conclusions_for_hypothesis("hv-rc") == (conclusion,)

    promotion = EvaluatePromotion(repository=fresh_after_conclusion).execute(
        EvaluatePromotionCommand(
            conclusion_id=conclusion.conclusion_id,
            policy=PromotionPolicy(),
            lookahead_violation=False,
            created_at=NOW,
        )
    )
    fresh_final = PostgresResearchMethodologyRepository(postgres_engine)
    reloaded_promotion = fresh_final.promotion_evaluation_by_id(promotion.promotion_evaluation_id)
    assert reloaded_promotion == promotion
    assert fresh_final.promotion_evaluations_for_conclusion(conclusion.conclusion_id) == (
        promotion,
    )
    # No holdout declared -- fails closed, exactly as the domain/application
    # layers already prove, now round-tripped through genuine Postgres.
    assert not promotion.eligible


def test_walk_forward_run_guard_blocks_identity_mutation(postgres_engine: Engine) -> None:
    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=100, idempotency_key="guard-wf-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    wf_run = CreateWalkForwardRun(
        repository=repository, historical_replay=replay_repository
    ).execute(
        CreateWalkForwardRunCommand(
            replay_run_id=run.replay_run_id,
            method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
            feature_policy_hash=run.feature_policy_hash,
            outcome_metric_id="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1",
            training_window_sessions=60,
            validation_window_sessions=0,
            test_window_sessions=20,
            step_sessions=20,
            window_mode=TrainingWindowMode.ROLLING,
            minimum_samples=5,
            cost_information_state="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL",
            start_date=run.start_date,
            end_date=run.end_date,
            hypothesis_version_id=None,
            operator_action_id="op-1",
            idempotency_key="guard-wf-run",
            created_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="identity fields are immutable"):
            connection.execute(
                sa.text(
                    "UPDATE walk_forward_run SET training_window_sessions = 999 "
                    "WHERE walk_forward_run_id = :run_id"
                ),
                {"run_id": wf_run.walk_forward_run_id},
            )
        connection.rollback()
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot be deleted"):
            connection.execute(
                sa.text("DELETE FROM walk_forward_run WHERE walk_forward_run_id = :run_id"),
                {"run_id": wf_run.walk_forward_run_id},
            )
        connection.rollback()
    # But cursor/completion/failure/payload fields are genuinely mutable --
    # exactly what save_walk_forward_run's own UPDATE path relies on.
    with postgres_engine.connect() as connection:
        connection.execute(
            sa.text(
                "UPDATE walk_forward_run SET cursor_fold_ordinal = 1, "
                "completion_state = 'IN_PROGRESS' WHERE walk_forward_run_id = :run_id"
            ),
            {"run_id": wf_run.walk_forward_run_id},
        )
        connection.commit()


def test_immutable_tables_reject_update_and_delete(postgres_engine: Engine) -> None:
    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=100, idempotency_key="immutable-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    trial = RecordResearchTrial(repository=repository, historical_replay=replay_repository).execute(
        RecordResearchTrialCommand(
            hypothesis_version_id="hv-immutable",
            dataset_snapshot_id=run.dataset_snapshot_id,
            replay_run_id=run.replay_run_id,
            phase=ResearchPhase.EXPLORATORY,
            market_scope=("US_EQUITY",),
            configuration=_configuration(),
            method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
            session_date_start=run.start_date,
            session_date_end=run.end_date,
            benchmark_kind=None,
            created_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                sa.text("UPDATE research_trial SET phase = 'LOCKED_HOLDOUT' WHERE trial_id = :id"),
                {"id": trial.trial_id},
            )
        connection.rollback()
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                sa.text("DELETE FROM research_trial WHERE trial_id = :id"), {"id": trial.trial_id}
            )
        connection.rollback()


def test_retained_evidence_blocks_destructive_0079_downgrade(postgres_engine: Engine) -> None:
    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=100, idempotency_key="downgrade-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    RecordResearchTrial(repository=repository, historical_replay=replay_repository).execute(
        RecordResearchTrialCommand(
            hypothesis_version_id="hv-downgrade",
            dataset_snapshot_id=run.dataset_snapshot_id,
            replay_run_id=run.replay_run_id,
            phase=ResearchPhase.EXPLORATORY,
            market_scope=("US_EQUITY",),
            configuration=_configuration(),
            method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
            session_date_start=run.start_date,
            session_date_end=run.end_date,
            benchmark_kind=None,
            created_at=NOW,
        )
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0079 with retained research methodology evidence",
        ):
            command.downgrade(_config(connection), "0078_historical_replay")
        connection.rollback()
    with postgres_engine.connect() as connection:
        # The multi-step downgrade cascades 0080 -> 0079 (succeeds, no
        # genuine multi-asset evidence retained in this test's scope) then
        # 0079 -> 0078 (rejected by the research-methodology guard above).
        # Each step commits independently, so the failure leaves
        # alembic_version at the last successfully completed step: 0079,
        # not the original head.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0079_quant_research_methodology"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0079_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_METHODOLOGY_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0078_historical_replay")
    assert set(RESEARCH_METHODOLOGY_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(RESEARCH_METHODOLOGY_TABLES)


def test_trial_reconstruction_is_idempotent_across_fresh_repository_instances(
    postgres_engine: Engine,
) -> None:
    """Genuine conflict detection (a corrupted retained row) is proven at
    the application layer against the in-memory repository -- it cannot be
    proven here, because ``research_trial`` is one of the genuinely
    immutable tables (see ``test_immutable_tables_reject_update_and_delete``
    above): the database itself refuses the row corruption a conflict test
    would need to construct, which is a *stronger* guarantee than the
    application-layer hash comparison alone. What Postgres *can* and should
    prove is that reconstructing the identical trial through a second,
    independent service instance is a genuine no-op, not a duplicate row."""

    replay_repository, _historical_repository, run = _build_replay_run(
        postgres_engine=postgres_engine, day_count=100, idempotency_key="idempotent-plan"
    )
    repository = PostgresResearchMethodologyRepository(postgres_engine)
    command = RecordResearchTrialCommand(
        hypothesis_version_id="hv-idempotent",
        dataset_snapshot_id=run.dataset_snapshot_id,
        replay_run_id=run.replay_run_id,
        phase=ResearchPhase.EXPLORATORY,
        market_scope=("US_EQUITY",),
        configuration=_configuration(),
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        session_date_start=run.start_date,
        session_date_end=run.end_date,
        benchmark_kind=None,
        created_at=NOW,
    )
    first = RecordResearchTrial(repository=repository, historical_replay=replay_repository).execute(
        command
    )
    fresh_repository = PostgresResearchMethodologyRepository(postgres_engine)
    second = RecordResearchTrial(
        repository=fresh_repository, historical_replay=replay_repository
    ).execute(command)
    assert first.trial_id == second.trial_id
    assert len(fresh_repository.trials_for_replay_run(run.replay_run_id)) == 1
